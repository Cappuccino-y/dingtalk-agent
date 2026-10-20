"""记忆存储层：append-only 的 markdown 记忆库 + 写入闸门 + 提案暂存区。

设计来源（Atelier 的 server/src/agents/memory.ts，同一套协议）：
- 一次只追加，不改写。删除/修订 = 追加一条 supersedes 旧条目的新条目。
- 每条带 provenance：source.messageIds / source.agentIds —— 谁说的、哪次对话来的。
- scope 决定文件归属，文件名做字符消毒，LLM 控制的 scope 永远逃不出 MEMORY_DIR。
- 写入门禁：凭据/密钥/证件号一律拒绝；敏感属性不推断；external（外部抓取）永不落盘。
- **有界**：每个 scope 最多 MAX_LEARNED_RULES 条活动记忆；pinned 条目是 IMMUTABLE，
  自动落地永远改不动。

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

# ---------------------------------------------------------------- 有界演化
# 每个 scope 的**活动**记忆条数上限。超出后 append_entry 直接拒绝 —— 不是
# 自动淘汰最旧的（那等于让循环悄悄删东西），而是让用户先合并/retire。
# 依据：自我改进系统最典型的失败模式是记忆单调增长 -> 全是噪音 -> 检索失效。
MAX_LEARNED_RULES = 60

# 打了这些 tag 的条目是「不可变层」：自动落地永远不能取代它们。
# 手工在 frontmatter 里加 tags: [pinned] 即可冻结。
IMMUTABLE_TAGS = ("pinned", "immutable")

# 判定两条记忆「说的是同一件事」的阈值（字符二元组 Jaccard）。
# 中文没有词边界，二元组比分词稳。0.55 实测能抓住改写过的同义条目。
DUP_THRESHOLD = 0.55

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
    """追加一条记忆。返回 memoryId。

    有界演化在这里落地：超出上限 / 想取代 IMMUTABLE 条目，一律抛 WriteRejected。
    **不会**自动淘汰旧条目 —— 删东西必须是显式动作。
    """
    if entry.category not in CATEGORIES:
        raise WriteRejected(f"未知 category: {entry.category}")
    if entry.confidence not in CONFIDENCES:
        raise WriteRejected(f"未知 confidence: {entry.confidence}")
    check_write(f"{entry.title}\n{entry.body}", trust=entry.trust, title=entry.title)

    actives = active_entries(entry.scope)

    # supersede 校验：目标必须存在、必须活动、必须不是 IMMUTABLE
    if entry.supersedes:
        tgt = next((e for e in actives if e["memoryId"] == entry.supersedes), None)
        if not tgt:
            raise WriteRejected(f"supersedes 目标不存在或已被取代: {entry.supersedes}")
        if tgt.get("immutable"):
            raise WriteRejected(
                f"目标 {entry.supersedes} 是 IMMUTABLE（tags 含 pinned），自动落地不能取代它")

    # 上限校验：取代是净 0，新增是 +1
    projected = len(actives) + (0 if entry.supersedes else 1)
    if projected > MAX_LEARNED_RULES:
        raise WriteRejected(
            f"{entry.scope} 的活动记忆已达上限 {MAX_LEARNED_RULES} 条（有界演化）。"
            f"请先合并旧条目并 retire/取代，再落新的。")

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
    # 反向索引：谁取代了谁。append-only 下这就是「删除/修订」的实现方式。
    sup_map = {}
    for e in out:
        if e.get("supersedes"):
            sup_map[e["supersedes"]] = e["memoryId"]
    for e in out:
        e["superseded_by"] = sup_map.get(e["memoryId"], "")
        e["immutable"] = bool(set(e["tags"]) & set(IMMUTABLE_TAGS))
    return out


def _filename_to_scope(name: str) -> str:
    """global.md -> global；project_x.md -> project:x（反向还原）。"""
    stem = name[:-3]
    if stem == "global":
        return "global"
    for kind in ("project", "room", "agent"):
        if stem.startswith(kind + "_"):
            return f"{kind}:{stem[len(kind) + 1:]}"
    return "global"


def read_entries(scope: str = "global") -> list[dict]:
    """读一个 scope 的**全部**条目（含已被取代的，带 superseded_by 标记）。"""
    return parse_file(scope_path(scope))


def active_entries(scope: str = "global") -> list[dict]:
    """只读活动条目（排除已被取代的）。索引 / 上限 / 相似度判断都用这个。"""
    return [e for e in read_entries(scope) if not e.get("superseded_by")]


def count_active(scope: str = "global") -> int:
    return len(active_entries(scope))


def all_active_entries() -> list[dict]:
    out = []
    if not os.path.isdir(MEMORY_DIR):
        return out
    for name in sorted(os.listdir(MEMORY_DIR)):
        if _is_memory_file(name):
            out.extend(active_entries(_filename_to_scope(name)))
    return out


def _is_memory_file(name: str) -> bool:
    """memory/ 下只有 global.md / project_*.md / room_*.md / agent_*.md 是记忆文件。

    README.md 之类的说明文档不能被当成记忆解析 —— 否则会被按 global 再算一遍。
    """
    if not name.endswith(".md"):
        return False
    stem = name[:-3]
    return stem == "global" or bool(re.match(r"^(project|room|agent)_[A-Za-z0-9_-]+$", stem))


def all_entries() -> list[dict]:
    out = []
    if not os.path.isdir(MEMORY_DIR):
        return out
    for name in sorted(os.listdir(MEMORY_DIR)):
        if _is_memory_file(name):
            out.extend(parse_file(os.path.join(MEMORY_DIR, name)))
    return out


def brief(scope: str = "global", limit: int = 40, body_chars: int = 90) -> str:
    """给提案器的对照用索引：标题 + 正文摘要。

    只给标题会让 LLM 误判「这条已经有了」——因为同一件事的标题措辞可以完全不同。
    给一小段正文，语义覆盖判断才靠得住（代价是多几百 token，值得）。
    """
    entries = active_entries(scope)
    if not entries:
        return "(暂无记忆)"
    lines = []
    for e in entries[-limit:]:
        body = re.sub(r"\s+", " ", e.get("body", "")).strip()
        if len(body) > body_chars:
            body = body[:body_chars] + "…"
        mark = " 📌" if e.get("immutable") else ""
        lines.append(f"- [{e['memoryId']}] ({e['category']}){mark} {e['title']} :: {body}")
    lines.append(f"（活动 {len(entries)}/{MAX_LEARNED_RULES} 条）")
    return "\n".join(lines)


def index(scope: str = "global", limit: int = 60) -> str:
    """记忆索引：只给 id/标题/类别，agent 需要细节时再读全文（省 context）。

    只列**活动**条目 —— 已被取代的不该再影响行为。
    """
    entries = active_entries(scope)
    if not entries:
        return "(暂无记忆)"
    lines = []
    for e in entries[-limit:]:
        mark = " 📌" if e.get("immutable") else ""
        lines.append(f"- [{e['memoryId']}] ({e['category']}/{e['confidence']}){mark} {e['title']}")
    lines.append(f"（活动 {len(entries)}/{MAX_LEARNED_RULES} 条）")
    return "\n".join(lines)


# --------------------------------------------------------------- 相似度 / 证据
def _bigrams(s: str) -> set:
    """字符二元组。中文没有词边界，二元组比分词稳，也不依赖任何分词库。"""
    s = re.sub(r"\s+", "", s or "")
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def similarity(a: str, b: str) -> float:
    """包含系数：短文本的二元组有多大比例出现在长文本里。

    为什么不用 Jaccard：两条记忆说同一件事但字数差一倍时，Jaccard 会被长度
    稀释（实测同义改写只有 0.10）。去重要问的是「短的那条是不是已经被覆盖」，
    这正是包含系数。过短的文本（<6 个二元组）不判，避免误命中。
    """
    A, B = _bigrams(a), _bigrams(b)
    if len(A) < 6 or len(B) < 6:
        return 0.0
    return len(A & B) / min(len(A), len(B))


def find_similar(text: str, scope: str | None = None,
                 threshold: float = 0.0, limit: int = 5) -> list[tuple[float, dict]]:
    """找最像的已存条目。scope=None 时跨全部 scope 找。

    **这是去重的确定性判据** —— 不靠 LLM 判断"这条是不是已经有了"。
    标题和正文分别算，取较大值（标题命中通常比正文更说明问题）。
    """
    pool = []
    if scope:
        pool = active_entries(scope)
    else:
        if os.path.isdir(MEMORY_DIR):
            for name in sorted(os.listdir(MEMORY_DIR)):
                if _is_memory_file(name):
                    pool.extend(active_entries(_filename_to_scope(name)))
    scored = []
    for e in pool:
        s_title = similarity(text, e["title"])
        s_full = similarity(text, f"{e['title']} {e['body']}")
        s = max(s_title, s_full)
        if s >= threshold:
            scored.append((s, e))
    scored.sort(key=lambda t: -t[0])
    return scored[:limit]


def evidence_seen(quote: str, threshold: float = 0.72,
                  exclude_pid: str = "") -> tuple[bool, str]:
    """这句「用户原话」是不是已经产出过提案了？（待审 / 批准 / 驳回都算见过）

    为什么用证据而不是标题做去重：标题/正文是 LLM 重写过的，同一件事能写成
    十种措辞，字符串相似度抓不住（实测同义改写只有 0.27）。但 **evidence 是
    用户的原文引用**，几乎逐字复现 —— 同一条要求的证据一定高度重合。

    exclude_pid：**批准时必须传自己的 id**，否则待审提案会匹配到自己，
    导致任何提案都过不了去重闸门。
    """
    if not quote or len(quote.strip()) < 8:
        return False, ""
    q_norm = re.sub(r"\s+", "", quote)
    cands = []
    # 待审 + 已批准 + 已驳回，三处都算「已经见过」
    for d in (PROPOSAL_DIR, os.path.join(PROPOSAL_DIR, "archive"), REJECTED_DIR):
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if not name.endswith(".json"):
                continue
            try:
                p = json.load(open(os.path.join(d, name), encoding="utf-8"))
            except Exception:
                continue
            if exclude_pid and p.get("id") == exclude_pid:
                continue
            ev = p.get("evidence") or ""
            if ev:
                cands.append((p.get("status", "?"), p.get("id", ""),
                              p.get("title", ""), ev))
    for status, pid, title, ev in cands:
        ev_norm = re.sub(r"\s+", "", ev)
        if not ev_norm:
            continue
        # 一边是另一边的子串（引用被截断/扩写）也算命中
        if q_norm in ev_norm or ev_norm in q_norm:
            return True, f"证据已被同一句原话的提案 {pid} 覆盖（{status}: {title[:40]}）"
        if similarity(q_norm, ev_norm) >= threshold:
            return True, f"证据与提案 {pid} 高度重合（{status}: {title[:40]}）"
    return False, ""


def match_evidence(quote: str, min_len: int = 8) -> list[str]:
    """把提案里的「用户原话引用」回溯到真实证据 id。

    返回形如 ["card:oc-xxx", "journal:2026-10-20T12:25:09"] 的引用列表。
    找不到就返回空 —— 不编造 id。
    """
    if not quote or len(quote.strip()) < min_len:
        return []
    q = re.sub(r"\s+", "", quote)
    refs: list[str] = []

    arc = os.path.join(REPO, "bot", "bridge_data", "card_archive.json")
    if os.path.exists(arc):
        try:
            data = json.load(open(arc, encoding="utf-8"))
            for c in (data.get("cards") or {}).values():
                blob = re.sub(r"\s+", "", f"{c.get('question', '')}{c.get('answer', '')}")
                # 用前 40 字做锚点，避免整段引用对不上
                if q[:40] in blob:
                    cid = c.get("card_id") or c.get("out_track_id") or ""
                    if cid:
                        refs.append(f"card:{cid}")
                    sid = c.get("session_id") or ""
                    if sid:
                        refs.append(f"session:{sid}")
                    if len(refs) >= 4:
                        break
        except Exception:
            pass

    if not refs:
        for r in read_journal(400):
            blob = re.sub(r"\s+", "", f"{r['summary']}{r['fields'].get('answer', '')}")
            if q[:40] in blob:
                refs.append(f"journal:{r['ts']}")
                break
    return refs


# --------------------------------------------------------------- journal
def append_acceptance(record: dict) -> None:
    """落地验收快照（append-only JSONL）。

    每次提案落地都记一条「改动前 / 改动后」，让 eval_memory --trend 能看到
    记忆库是在变好还是只是变大。放 JSONL 而不是 md，因为它是机器读的。
    """
    os.makedirs(MEMORY_DIR, exist_ok=True)
    path = os.path.join(MEMORY_DIR, "acceptance.jsonl")
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **record}
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_acceptance(limit: int = 200) -> list[dict]:
    path = os.path.join(MEMORY_DIR, "acceptance.jsonl")
    if not os.path.exists(path):
        return []
    out = []
    for ln in open(path, encoding="utf-8"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out[-limit:]


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
    elif cmd == "bounds":
        for sc in ("global", "project:dingtalk-agent"):
            a = active_entries(sc)
            sup = [e for e in read_entries(sc) if e.get("superseded_by")]
            pin = [e for e in a if e.get("immutable")]
            print(f"{sc}: 活动 {len(a)}/{MAX_LEARNED_RULES}  已取代 {len(sup)}  pinned {len(pin)}")
    elif cmd == "similar":
        # python tools/memory_store.py similar "一段文字"
        import sys as _s
        txt = _s.argv[2] if len(_s.argv) > 2 else ""
        for score, e in find_similar(txt, threshold=0.0, limit=5):
            print(f"  {score:.3f}  [{e['memoryId']}] {e['title']}")
