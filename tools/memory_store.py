"""记忆存储层：append-only 的 markdown 记忆库 + 写入闸门 + 提案暂存区。

设计来源（Atelier 的 server/src/agents/memory.ts，同一套协议）：
- 一次只追加，不改写。删除/修订 = 追加一条 supersedes 旧条目的新条目。
- 每条带 provenance：source.messageIds / source.agentIds —— 谁说的、哪次对话来的。
- scope 决定文件归属，文件名做字符消毒，LLM 控制的 scope 永远逃不出 MEMORY_DIR。
- 写入门禁：凭据/密钥/证件号一律拒绝；敏感属性不推断；external（外部抓取）永不落盘。

三层信任（对应 ApertoMemory 的 trust level / TMA-NM 的 origin binding）：
    self     用户明确说的     -> 可进记忆，可影响行为
    observed 工具/数据源看到的 -> 可进记忆（带 as_of），只作上下文
    external 网页/新闻/抓取    -> 永不进记忆
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field, asdict

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
MEMORY_DIR = os.path.join(REPO, "memory")
PROPOSAL_DIR = os.path.join(MEMORY_DIR, "proposals")
REJECTED_DIR = os.path.join(MEMORY_DIR, "rejected")
JOURNAL_PATH = os.path.join(MEMORY_DIR, "journal.md")
PROFILE_PATH = os.path.join(MEMORY_DIR, "profile.md")

# 允许进记忆的类别（Atelier 同款）
CATEGORIES = ("pattern", "decision", "gotcha", "template", "fact", "preference")
CONFIDENCES = ("high", "medium", "low")
TRUSTS = ("self", "observed")

# 写入门禁：命中即拒绝，不做"脱敏后照存"
_FORBIDDEN = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "openai-style api key"),
    (re.compile(r"ark-[A-Za-z0-9_\-]{16,}"), "volcengine api key"),
    (re.compile(r"gho_[A-Za-z0-9]{20,}"), "github token"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "github token"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "aws access key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)\s*[:=]\s*\S{6,}"),
     "credential-like assignment"),
    (re.compile(r"\b\d{15,19}\b"), "possible card/account number"),
    (re.compile(r"\b\d{17}[\dXx]\b"), "possible id card number"),
)
# 敏感属性：只允许用户明说（trust=self），不接受推断
# 注意：CJK 没有 \b 词边界，中文词条不能用 \b 包裹，否则永远匹配不上
_SENSITIVE_CJK = re.compile(
    r"(健康|疾病|诊断|抑郁|焦虑|精神疾病|宗教|信仰|党员|政治立场|性取向|"
    r"残疾|慢性病|过敏|用药)")
_SENSITIVE_EN = re.compile(
    r"(?i)\b(diagnos(is|ed)|depression|anxiety|mental health|"
    r"religious (belief|affiliation)|sexual orientation|chronic disease|"
    r"disability)\b")


class WriteRejected(Exception):
    """写入门禁拒绝。reason 说明拒了什么。"""


def sanitize_scope_segment(raw: str) -> str:
    """scope 片段消毒：只留 [a-zA-Z0-9_-]，防目录穿越。"""
    out = re.sub(r"[^a-zA-Z0-9_-]", "_", (raw or "").strip())[:64]
    return out or "default"


def scope_to_filename(scope: str) -> str:
    """scope 字符串 -> 文件名。global / project:<name> / room:<id> / agent:<id>。"""
    scope = (scope or "global").strip()
    if scope == "global":
        return "global.md"
    kind, _, rest = scope.partition(":")
    kind = kind.lower()
    if kind in ("project", "room", "agent"):
        return f"{kind}_{sanitize_scope_segment(rest)}.md"
    return "global.md"


def scope_path(scope: str) -> str:
    return os.path.join(MEMORY_DIR, scope_to_filename(scope))


@dataclass
class Entry:
    scope: str = "global"
    category: str = "pattern"
    title: str = ""
    body: str = ""
    tags: list[str] = field(default_factory=list)
    confidence: str = "medium"
    trust: str = "self"
    source_message_ids: list[str] = field(default_factory=list)
    source_agent_ids: list[str] = field(default_factory=list)
    supersedes: str = ""
    memory_id: str = ""
    timestamp: str = ""

    def to_frontmatter(self) -> str:
        mid = self.memory_id or f"mem_{uuid.uuid4().hex[:8]}_{int(time.time()) % 100000:05d}"
        self.memory_id = mid
        ts = self.timestamp or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.timestamp = ts
        src_m = ", ".join(self.source_message_ids) or "-"
        src_a = ", ".join(self.source_agent_ids) or "-"
        lines = [
            "---",
            f"memoryId: {mid}",
            f"timestamp: {ts}",
            f"scope: {self.scope}",
            f"category: {self.category}",
            f"title: {self.title}",
            f"tags: [{', '.join(self.tags)}]",
            f"confidence: {self.confidence}",
            f"trust: {self.trust}",
            f"source.messageIds: {src_m}",
            f"source.agentIds: {src_a}",
        ]
        if self.supersedes:
            lines.append(f"supersedes: {self.supersedes}")
        lines += ["---", "", self.body.strip(), ""]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["memory_id"] = self.memory_id
        return d


def check_write(text: str, trust: str = "self", title: str = "") -> None:
    """写入门禁。命中即抛 WriteRejected —— 不是脱敏后照存，是不存。"""
    if trust not in TRUSTS:
        raise WriteRejected(f"trust={trust!r} 不允许落盘（external 内容永不进记忆）")
    blob = f"{title}\n{text}"
    for rx, why in _FORBIDDEN:
        if rx.search(blob):
            raise WriteRejected(f"疑似 {why}，拒绝写入")
    if trust != "self" and (_SENSITIVE_CJK.search(blob) or _SENSITIVE_EN.search(blob)):
        raise WriteRejected("敏感属性只能由用户明说（trust=self）时记录，工具推断的一律不记")


def append_entry(entry: Entry) -> str:
    """追加一条记忆。返回 memoryId。"""
    if entry.category not in CATEGORIES:
        raise WriteRejected(f"未知 category: {entry.category}")
    if entry.confidence not in CONFIDENCES:
        raise WriteRejected(f"未知 confidence: {entry.confidence}")
    check_write(f"{entry.title}\n{entry.body}", trust=entry.trust, title=entry.title)
    os.makedirs(MEMORY_DIR, exist_ok=True)
    path = scope_path(entry.scope)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(entry.to_frontmatter() + "\n")
    return entry.memory_id


_FM_KEYS = ("memoryId", "timestamp", "scope", "category", "title", "tags",
            "confidence", "trust", "source.messageIds", "source.agentIds", "supersedes")

# 一条记忆 = "---\n<frontmatter>\n---\n<body>"，到下一个 --- 或文件末尾为止
_ENTRY_RE = re.compile(r"(?ms)^---\s*\n(.*?)^---\s*\n(.*?)(?=^---\s*\n|\Z)")


def parse_file(path: str) -> list[dict]:
    """解析一个记忆文件。"""
    if not os.path.exists(path):
        return []
    text = open(path, encoding="utf-8").read()
    out: list[dict] = []
    for fm, body in _ENTRY_RE.findall(text):
        meta = {}
        for ln in fm.splitlines():
            if ":" in ln:
                k, _, v = ln.partition(":")
                k = k.strip()
                if k in _FM_KEYS:
                    meta[k] = v.strip()
        if not meta.get("memoryId"):
            continue
        out.append({
            "memoryId": meta.get("memoryId", ""),
            "timestamp": meta.get("timestamp", ""),
            "scope": meta.get("scope", "global"),
            "category": meta.get("category", "pattern"),
            "title": meta.get("title", ""),
            "tags": [t.strip() for t in meta.get("tags", "").strip("[]").split(",") if t.strip()],
            "confidence": meta.get("confidence", "medium"),
            "trust": meta.get("trust", "self"),
            "source.messageIds": meta.get("source.messageIds", "-"),
            "source.agentIds": meta.get("source.agentIds", "-"),
            "supersedes": meta.get("supersedes", ""),
            "body": body.strip(),
        })
    return out


def read_entries(scope: str = "global") -> list[dict]:
    return parse_file(scope_path(scope))


def all_entries() -> list[dict]:
    out = []
    if not os.path.isdir(MEMORY_DIR):
        return out
    for name in os.listdir(MEMORY_DIR):
        if name.endswith(".md") and name not in ("journal.md", "profile.md"):
            out.extend(parse_file(os.path.join(MEMORY_DIR, name)))
    return out


def index(scope: str = "global", limit: int = 60) -> str:
    """记忆索引：只给 id/标题/类别，agent 需要细节时再读全文（省 context）。"""
    entries = read_entries(scope)
    if not entries:
        return "(暂无记忆)"
    lines = []
    for e in entries[-limit:]:
        lines.append(f"- [{e['memoryId']}] ({e['category']}/{e['confidence']}) {e['title']}")
    return "\n".join(lines)


# --------------------------------------------------------------- journal
def append_journal(kind: str, summary: str, ts: str = "", **extra) -> None:
    """append-only 证据流水：每次交互/反馈都记一行，只追加不修改。

    ts 允许传入**事件真实发生的时间**（回填历史时用），默认取当前时间。
    """
    os.makedirs(MEMORY_DIR, exist_ok=True)
    stamp = ts or time.strftime("%Y-%m-%dT%H:%M:%S")
    parts = [f"## {stamp} — {kind}", f"- Summary: {summary}"]
    for k, v in extra.items():
        if v:
            parts.append(f"- {k}: {v}")
    with open(JOURNAL_PATH, "a", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(parts) + "\n\n")


def read_journal(limit: int = 200) -> list[dict]:
    """读 journal 最近 N 条（按 '## ' 分块）。"""
    if not os.path.exists(JOURNAL_PATH):
        return []
    text = open(JOURNAL_PATH, encoding="utf-8").read()
    blocks = [b for b in re.split(r"(?m)^## ", text) if b.strip()]
    out = []
    for b in blocks[-limit:]:
        lines = b.splitlines()
        rec = {"ts": lines[0].strip(), "kind": "", "summary": "", "fields": {}}
        for ln in lines[1:]:
            if ln.startswith("- Summary:"):
                rec["summary"] = ln[len("- Summary:"):].strip()
            elif ln.startswith("- "):
                k, _, v = ln[2:].partition(":")
                rec["fields"][k.strip()] = v.strip()
        rec["kind"] = rec["fields"].pop("Kind", "") or rec["ts"]
        out.append(rec)
    return out


# --------------------------------------------------------------- proposals
def proposal_path(pid: str) -> str:
    return os.path.join(PROPOSAL_DIR, f"{pid}.json")


def save_proposal(p: dict) -> str:
    os.makedirs(PROPOSAL_DIR, exist_ok=True)
    pid = p.get("id") or f"prop_{time.strftime('%Y%m%d-%H%M%S')}_{uuid.uuid4().hex[:4]}"
    p["id"] = pid
    p.setdefault("created_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    p.setdefault("status", "pending")
    with open(proposal_path(pid), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(p, fh, ensure_ascii=False, indent=2)
    return pid


def load_proposal(pid: str) -> dict | None:
    path = proposal_path(pid)
    if not os.path.exists(path):
        return None
    return json.load(open(path, encoding="utf-8"))


def list_proposals(status: str = "pending") -> list[dict]:
    if not os.path.isdir(PROPOSAL_DIR):
        return []
    out = []
    for name in sorted(os.listdir(PROPOSAL_DIR)):
        if not name.endswith(".json"):
            continue
        try:
            p = json.load(open(os.path.join(PROPOSAL_DIR, name), encoding="utf-8"))
        except Exception:
            continue
        if status and p.get("status") != status:
            continue
        out.append(p)
    return out


def archive_proposal(pid: str, status: str, note: str = "") -> str:
    """把提案从 pending 挪走：approved -> proposals/archive/，rejected -> rejected/。"""
    src = proposal_path(pid)
    if not os.path.exists(src):
        raise FileNotFoundError(pid)
    p = json.load(open(src, encoding="utf-8"))
    p["status"] = status
    p["decided_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if note:
        p["decision_note"] = note
    if status == "rejected":
        os.makedirs(REJECTED_DIR, exist_ok=True)
        dst = os.path.join(REJECTED_DIR, f"{pid}.json")
    else:
        dst = os.path.join(PROPOSAL_DIR, "archive", f"{pid}.json")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(p, fh, ensure_ascii=False, indent=2)
    os.remove(src)
    return dst


def rejected_summaries(limit: int = 10) -> list[str]:
    """最近被驳回的提案摘要 —— 下次提议时要避开，别反复提同一个坑。"""
    if not os.path.isdir(REJECTED_DIR):
        return []
    out = []
    for name in sorted(os.listdir(REJECTED_DIR), reverse=True)[:limit]:
        if not name.endswith(".json"):
            continue
        try:
            p = json.load(open(os.path.join(REJECTED_DIR, name), encoding="utf-8"))
        except Exception:
            continue
        out.append(f"{p.get('id')} :: {p.get('title', '')}")
    return out


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "index"
    if cmd == "index":
        for sc in ("global", "project:dingtalk-agent"):
            print(f"--- {sc} ---")
            print(index(sc))
    elif cmd == "all":
        print(json.dumps(all_entries(), ensure_ascii=False, indent=2))
    elif cmd == "journal":
        for r in read_journal():
            print(r["ts"], "|", r["summary"])
