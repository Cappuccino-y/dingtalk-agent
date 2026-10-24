"""evolve —— 每日/定期的「知识与行为改进扫描」作业。

跑在 scheduler 的短命子进程里（跑完就退），做三件事：

  1. 读证据：journal（交互流水）+ 记忆索引 + 被驳回的提案 + 最近的问答存档
  2. 让 LLM 提改进提案（**直连大模型，没有任何工具** —— 物理上改不了任何文件）
  3. 把提案存进 memory/proposals/，推一张钉钉审批卡给用户

**关键隔离（Outside-the-Loop Invariant）**：
- 提案者用 llm_client 直连 API，**不经过 opencode** → 没有 read/write/bash/edit 任何工具，
  也就改不了记忆库、改不了自己的评判标准。
- 落地由 tools/apply_proposal.py 这个确定性代码完成，**不经 LLM**。
- evaluator 在 loop 外（tools/eval_memory.py），提案者看不到它。

用法：
    python tools/evolve.py            # 扫描 + 提提案 + 推审批卡
    python tools/evolve.py --dry-run  # 只打印提案，不落盘、不推送
    python tools/evolve.py --no-push  # 落盘但不推送（留给手动审）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools import memory_store as ms  # noqa: E402

MAX_PROPOSALS = 3
RECENT_JOURNAL = 80
RECENT_CARDS = 20

# 「已经做完」的标记。journal 里出现这些词的条目 = 当时已经把事情办了，
# 它们是提案器的**对照表**（别再把做完的事当新问题提一遍）。
DONE_MARKERS = ("已落地", "已修复", "全部修复", "全部改动", "全部改", "已完成",
                "已改", "已加", "已撤回", "已实现", "已生效", "已修好")

SYSTEM_PROMPT = """你是一个 AI 助理系统的「需求抽取器」。

**你的任务只有一件（机械对照，不要发挥）**：
把「证据」里用户**明确提出的要求 / 纠正 / 偏好**逐条挑出来，
再和「现有记忆索引」对照，找出**没有被覆盖**的那些 —— 只有这些才是提案。

严禁做的事：
1. **不要评价项目是否完成、是否完善、还有什么可做的** —— 你没有这个判断能力。
   （本系统里的模型如果这么回答，说明它没在做对照，在空想。）
2. 不要提你没在证据里看到的东西。
3. 不要提"删记忆"以外的破坏性改动。

重点找这些（用户原话里出现的）：
- 祈使句要求："要能…""应该…""以后…""必须…""别…""不要…"
- 明确纠正："不是…""应该是…""错了""改成"
- 稳定偏好："我一般…""我习惯…""以后都…"
- 踩过的坑："刚才…出错了""又…了"

对照规则：
- 如果记忆索引里已有同类条目（标题**或正文**覆盖了这条要求）→ **不提案**
- 如果「已处理过的提案」或「近期已完成改动」里已经出现过这条要求 → **不提案**
- 证据里每条问答的 **A（回答）= 当时实际的处理结果**。
  回答里已经说明「已改 / 已撤回 / 已实现 / 已加规则 / 已在代码里落地」的要求
  = 当时就办完了 → **不要再提**（这是 2026-10-24 修的老毛病：
  用户提过的要求当场改完了，提案器却因为记忆索引里没这条而反复重提）
- 如果没有对应条目 → 提案，并在 evidence 里**原文引用**用户的原话
- 如果新要求和某条已有条目**冲突/是它的修正**（比如用户后来改了口径）→
  提案时填 `supersedes: <那条的 memoryId>`，body 写**修正后的完整内容**
- 不要因为"想补充细节"就 supersede；只在口径真的变了时才这么做

硬约束（系统会强制检查，违反的直接丢弃）：
- 同一个 scope 最多 60 条活动记忆。快满了就优先提「合并/废弃」，不要硬塞新的
- 打了 📌 的条目是 IMMUTABLE，**不要**提议取代它

提案类型：
- memory      —— 该沉淀成长期知识的坑/事实/模式
- convention —— 该写进「用户画像/工作方式」的稳定约定

其他硬规则：
- 涉及金融动作（具体买卖点位、仓位、止损数字）的一律不提
- 涉及凭据、密钥、身份证号的不提
- 外部抓取内容（网页、新闻、行情）不能作为记忆来源
- 一次最多 3 个；没有就返回空数组，**不要为了凑数而提**
- **只提还没做的事**：凡是「已处理过的提案」「近期已完成改动」里出现过的，
  或者是证据里回答已经说明「已改/已撤回/已实现」的，都不要再提一遍
  （提了就是让用户重复确认已经确认过的事 —— 这是最招人烦的失败模式）

只输出 JSON，不要解释、不要 markdown 代码块：
{"proposals": [
  {"type": "memory", "title": "一句话标题",
   "scope": "global" 或 "project:dingtalk-agent",
   "category": "pattern|decision|gotcha|template|fact|preference",
   "confidence": "high|medium|low",
   "body": "触发条件: ...\\n动作: ...\\n适用范围: ...",
   "evidence": "用户原话引用",
   "supersedes": "要取代的 memoryId（没有就省略或留空）",
   "risk": "落地后可能有什么副作用",
   "expected_gain": "预期改善"}
]}"""


def done_items(journal: list[dict], limit: int = 12) -> list[str]:
    """近期「已经做完」的事 —— 提案器的对照表。

    2026-10-24 加：用户反馈「evolve 提的全是之前已经改过的东西」。
    根因是提案器只有「记忆索引」一个对照源，而很多改动是在对话里当场改的
    （IC Monitor 的三条防误判规则就是），没进记忆索引 → 每次扫描都当成新问题。
    这里把 journal 里明确写了「已落地/已修复/全部修复/已改」这类完成标记的
    条目连同当时的回答摘要喂进去，让模型能对照出「这件事已经办过了」。
    """
    out: list[str] = []
    for r in journal:
        s = r.get("summary", "") or ""
        if not any(m in s for m in DONE_MARKERS):
            continue
        ans = (r.get("fields", {}) or {}).get("answer", "") or ""
        line = f"- [{r.get('ts', '')}] {s[:140]}"
        if ans and ans != "-":
            line += f"  →  {ans[:200]}"
        out.append(line)
    return out[-limit:]


def collect_evidence() -> dict:
    """收集证据。全部是**结构化摘要**，不塞原始大段文本（省 token 也避免污染）。

    问答存档只用 held-in 那一半（奇数位）—— held-out 是验收用的冻结核，
    提案器看过的例子就不能再当判例（否则验收是自证）。
    """
    journal = ms.read_journal(RECENT_JOURNAL)
    rejected = ms.rejected_summaries(10)
    # 已经处理过的提案（驳回/暂缓/已落地）—— 2026-10-24 扩：原来只喂「被驳回」的，
    # 结果「暂缓」和「已落地」的主题会被当成新问题重提。
    handled = ms.handled_summaries(20)
    # 正文摘要而不是只给标题：标题措辞差异大，只给标题会让模型误判「已覆盖」
    idx_global = ms.brief("global", limit=40)
    idx_project = ms.brief("project:dingtalk-agent", limit=40)

    cards = []
    try:
        from tools import eval_acceptance as ea
        # 回填进 journal 的历史问答里含 held-out 判例，证据侧要剔掉（防泄漏）
        ho = ea.heldout_fingerprints()
        journal = [r for r in journal
                   if not (r["fields"].get("source") == "card_archive"
                           and f"{r['ts']}|{r['summary'][:200]}" in ho)]
        for c in ea.evidence_cards(RECENT_CARDS):
            cards.append({
                "q": (c.get("question") or "")[:200],
                "a": (c.get("answer") or "")[:400],
                "session": c.get("session_id", ""),
            })
    except Exception as e:  # noqa: BLE001
        print(f"[evolve] 读取 held-in 判例失败: {e}", file=sys.stderr)

    return {
        "journal": journal,
        "rejected_proposals": rejected,
        "handled_proposals": handled,
        "done_items": done_items(journal),
        "memory_index_global": idx_global,
        "memory_index_project": idx_project,
        "recent_qa": cards,
    }


def build_prompt(ev: dict) -> str:
    parts = ["# 证据\n"]
    if ev["journal"]:
        parts.append("## 交互流水 journal（最近 %d 条）" % len(ev["journal"]))
        parts.append("每条格式：时间 | 用户说了什么 | 回答摘要")
        for r in ev["journal"]:
            parts.append(f"- [{r['ts']}] {r['summary']}")
            ans = r["fields"].get("answer", "")
            if ans and ans != "-":
                parts.append(f"    (回答) {ans[:200]}")
    else:
        parts.append("## 交互流水 journal\n（空）")
    if ev["rejected_proposals"]:
        parts.append("\n## 之前被驳回的提案（**不要再提这些**）")
        for s in ev["rejected_proposals"]:
            parts.append(f"- {s}")
    if ev.get("handled_proposals"):
        parts.append("\n## 已处理过的提案（驳回 / 暂缓 / 已落地 —— **全都不要再提**）")
        for s in ev["handled_proposals"]:
            parts.append(s)
    if ev.get("done_items"):
        parts.append("\n## 近期已经做完的事（**这些都办过了，别再当新问题提**）")
        for s in ev["done_items"]:
            parts.append(s)
    parts.append("\n## 现有记忆索引 global（**已被覆盖的不要提**）")
    parts.append(ev["memory_index_global"])
    parts.append("\n## 现有记忆索引 project:dingtalk-agent（**已被覆盖的不要提**）")
    parts.append(ev["memory_index_project"])
    if ev["recent_qa"]:
        parts.append("\n## 最近的问答存档（补充细节用）")
        parts.append("注意：每条 A（回答）= 当时实际的处理结果。"
                     "回答里说「已改 / 已撤回 / 已实现」的，说明当时就办完了。")
        for c in ev["recent_qa"]:
            parts.append(f"Q: {c['q']}\nA: {c['a']}\n")
    return "\n".join(parts)


def call_llm(prompt: str) -> tuple[str, str]:
    """调 LLM 提提案。返回 (原始输出, 实际用的模型)。

    主模型失败（欠费/限流/超时）时自动降级到备用模型 —— 这是**每天凌晨无人值守**
    跑的作业，不能因为一次抽风就整晚不产出。
    """
    from tools.llm_client import OpenAICompatibleClient
    primary = os.getenv("EVOLVE_LLM_MODEL", "MiniMax-M3.1-Flash")
    fallback = os.getenv("EVOLVE_LLM_FALLBACK_MODEL", "MiniMax-M3")
    client = OpenAICompatibleClient.for_job("EVOLVE")
    print(f"[evolve] LLM: model={client.model} base={getattr(client.client, 'base_url', '?')}")
    try:
        return client.generate(prompt, system_prompt=SYSTEM_PROMPT), client.model
    except Exception as e:  # noqa: BLE001
        print(f"[evolve] 主模型 {primary} 失败：{e}", file=sys.stderr)
        if not fallback or fallback == primary:
            raise
        fb = OpenAICompatibleClient(model=fallback,
                                    api_key=os.getenv("MINIMAX_API_KEY"),
                                    base_url=os.getenv("EVOLVE_LLM_BASE_URL")
                                    or "https://api.minimaxi.com/v1")
        print(f"[evolve] 降级到 {fallback}", file=sys.stderr)
        return fb.generate(prompt, system_prompt=SYSTEM_PROMPT), fallback


_THINK_RE = None


def strip_think(text: str) -> str:
    """去掉 <think>...</think> 推理块（部分模型会把思考混进正文）。"""
    global _THINK_RE
    if _THINK_RE is None:
        import re
        _THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
    return _THINK_RE.sub("", text or "").strip()


def parse_proposals(raw: str) -> list[dict]:
    """从 LLM 输出里抠出 JSON。容忍 <think> 块、```json 包裹和前后废话。"""
    if not raw:
        return []
    s = strip_think(raw)
    if "```" in s:
        parts = s.split("```")
        for p in parts:
            p = p.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{") or p.startswith("["):
                s = p
                break
    start = s.find("{")
    end = s.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        obj = json.loads(s[start:end + 1])
    except Exception:
        return []
    props = obj.get("proposals") if isinstance(obj, dict) else obj
    if not isinstance(props, list):
        return []
    out = []
    for p in props:
        if not isinstance(p, dict):
            continue
        if not p.get("title") or not p.get("body"):
            continue
        if p.get("type") not in ("memory", "convention"):
            continue
        if p.get("category", "pattern") not in ms.CATEGORIES:
            p["category"] = "pattern"
        if p.get("confidence", "medium") not in ms.CONFIDENCES:
            p["confidence"] = "medium"
        # supersedes 只接受 memoryId 形状，其他一律清空（防止 LLM 编个路径来穿越）
        sup = str(p.get("supersedes") or "").strip()
        p["supersedes"] = sup if re.fullmatch(r"mem_[A-Za-z0-9_]+", sup) else ""
        out.append(p)
    return out[:MAX_PROPOSALS]


# --------------------------------------------------------------- 审批卡
def _recipient():
    path = os.path.join(REPO, "tools", ".dingtalk_recipient.json")
    if not os.path.exists(path):
        return None
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return None


class _Msg:
    """给 card_v2 用的最小 msg 替身（只需要几个属性）。"""
    def __init__(self, r):
        self.conversation_id = r.get("conversation_id", "")
        self.sender_staff_id = r.get("sender_staff_id", "")
        self.conversation_type = r.get("conversation_type", "1")
        self.robot_code = r.get("robot_code", "")


def _mark_pushed(pids) -> None:
    """记一下每条提案最近一次推卡的时间（控制"再问一次"的间隔）。"""
    for pid in pids or []:
        p = ms.load_proposal(pid)
        if not p:
            continue
        p["card_pushed_ts"] = time.time()
        p["card_pushed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(ms.proposal_path(pid), "w", encoding="utf-8", newline="\n") as fh:
            json.dump(p, fh, ensure_ascii=False, indent=2)


# 上一轮没批的提案，隔多久再问一次
REASK_HOURS = 6


def _pending_for_reask() -> list[tuple[str, dict]]:
    """还挂着 pending、但上次推卡已经超过 REASK_HOURS 的提案 → 这一轮再问一次。

    "稍后再说"（defer）现在**不冻结**提案（状态仍是 pending），
    所以必须有个"再问"的机制，否则它们永远没人管。
    """
    out = []
    now = time.time()
    for p in ms.list_proposals("pending"):
        ts = p.get("card_pushed_ts")
        if isinstance(ts, (int, float)) and (now - ts) < REASK_HOURS * 3600:
            continue
        pid = p.get("id") or ""
        if pid:
            out.append((pid, p))
    return out


def _push_cards(new_items: list[tuple[str, dict]]) -> int:
    """推审批卡：新提案 + 上一轮没批、放凉超过 REASK_HOURS 的老提案。

    一律合成**一张**批量卡（一条提案一个下拉），超过 4 条分批推。
    """
    items = list(new_items or [])
    fresh = {pid for pid, _ in items}
    for pid, p in _pending_for_reask():
        if pid not in fresh:
            items.append((pid, p))
    if not items:
        return 0
    CHUNK = 4
    n = 0
    for i in range(0, len(items), CHUNK):
        if push_approval_batch_card(items[i:i + CHUNK]):
            n += 1
    return n


def push_approval_batch_card(items: list[tuple[str, dict]]) -> bool:
    """推**一张**批量审批卡：一条提案一个选项，一次提交全部结算。

    为什么不再一条提案一张卡（2026-10-22 改）：
    - 手机上 3 张卡要分别点开、分别提交，用户得记住"还剩几张没批"；
    - 更糟的是钉钉在卡片渲染时会回调一次 fromConfig 事件，
      老逻辑把它当成"用户作答"，于是在用户还没碰第二张卡的时候，
      第二张卡就被判成"❓ 没看懂你的选择"并收尾 —— 提案其实还挂着。
    合成一张卡以后：只推一次、只收一次回调，也没有"哪张卡被提前判死"的问题。

    items: [(pid, prop), ...]，顺序 = 表单域顺序（p0 / p1 / ...）。
    question_id 编码全部 pid（无状态、可跨进程）：evolve:batch:<pid1>,<pid2>
    """
    if not items:
        return False
    r = _recipient()
    if not r or not r.get("conversation_id"):
        print("[evolve] 没有收件人信息，跳过推送", file=sys.stderr)
        return False
    try:
        from tools import card_v2
        if not getattr(card_v2, "ASK_CARD_ENABLED", False):
            print("[evolve] ASK 卡片未启用，跳过推送", file=sys.stderr)
            return False
        opts = [{"label": "批准", "value": "approve"},
                {"label": "驳回", "value": "reject"},
                {"label": "稍后再说", "value": "defer"}]
        fields = []
        desc_lines = []
        for i, (pid, prop) in enumerate(items):
            f = card_v2.ask_field("p%d" % i, "%d. %s" % (i + 1, (prop.get("title") or "")[:18]), opts)
            if not f:
                continue
            fields.append(f)
            desc_lines.append(
                "**%d. %s**\n类型: %s ｜ 目标: %s\n依据: %s\n风险: %s\n%s"
                % (i + 1, (prop.get("title") or "")[:40], prop.get("type", "-"),
                   prop.get("scope", "global"), (prop.get("evidence") or "-")[:120],
                   (prop.get("risk") or "-")[:120], (prop.get("body") or "")[:220]))
        if not fields:
            return False
        pids = [pid for pid, _ in items][:len(fields)]
        desc = ("以下 %d 条提案，请逐条选择后一次性提交：\n\n%s"
                % (len(fields), "\n\n".join(desc_lines)))
        card = card_v2.AskCard(_Msg(r), "evolve:batch:" + ",".join(pids),
                               title="改进提案 · %d 条待批" % len(fields),
                               desc=desc, fields=fields, submit_text="提交决定")
        ok = card.create()
        _mark_pushed(pids)
        print("[evolve] 批量审批卡已投递 n=%d pids=%s outTrackId=%s ok=%s"
              % (len(fields), ",".join(pids), card.out_track_id, ok))
        return ok
    except Exception as e:  # noqa: BLE001
        print(f"[evolve] 批量审批卡投递失败: {e}", file=sys.stderr)
        return False


def push_approval_card(pid: str, prop: dict) -> bool:
    """推审批卡。用 ASK 卡片（form SELECT），question_id 里编码提案 id —— 无状态、可跨进程。"""
    r = _recipient()
    if not r or not r.get("conversation_id"):
        print("[evolve] 没有收件人信息，跳过推送", file=sys.stderr)
        return False
    try:
        from tools import card_v2
        if not getattr(card_v2, "ASK_CARD_ENABLED", False):
            print("[evolve] ASK 卡片未启用，跳过推送", file=sys.stderr)
            return False
        fields = [card_v2.ask_field(
            "decision", "你的决定",
            [{"label": "批准", "value": "approve"},
             {"label": "驳回", "value": "reject"},
             {"label": "稍后再说", "value": "defer"}])]
        if not fields or not fields[0]:
            return False
        desc = (f"类型: {prop.get('type')}\n"
                f"目标: {prop.get('scope', 'global')}\n"
                f"依据: {prop.get('evidence', '-')}\n"
                f"风险: {prop.get('risk', '-')}\n"
                f"预期: {prop.get('expected_gain', '-')}\n\n"
                f"{prop.get('body', '')}")
        card = card_v2.AskCard(_Msg(r), f"evolve:{pid}",
                               title=f"改进提案 · {prop.get('title', '')[:40]}",
                               desc=desc, fields=fields, submit_text="提交决定")
        ok = card.create()
        print(f"[evolve] 审批卡已投递 pid={pid} outTrackId={card.out_track_id} ok={ok}")
        return ok
    except Exception as e:  # noqa: BLE001
        print(f"[evolve] 审批卡投递失败: {e}", file=sys.stderr)
        return False


def backfill_cards() -> int:
    """把 card_archive.json 里的历史问答回填进 journal。

    这些是**真实发生过的交互**，不是编的证据 —— 迁移后历史对话留在旧仓，
    回填一次就能让改进扫描立刻有素材（否则要等新交互攒够）。
    """
    arc = os.path.join(REPO, "bot", "bridge_data", "card_archive.json")
    if not os.path.exists(arc):
        print("[evolve] 没有 card_archive.json，跳过回填")
        return 0
    try:
        data = json.load(open(arc, encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"[evolve] card_archive 读取失败: {e}", file=sys.stderr)
        return 0
    items = list((data.get("cards") or {}).values())
    items.sort(key=lambda c: c.get("at", 0))
    n = 0
    for c in items:
        q = (c.get("question") or "").strip()
        a = (c.get("answer") or "").strip()
        if not q and not a:
            continue
        at = c.get("at")
        ts = ""
        if isinstance(at, (int, float)) and at > 0:
            ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(at))
        ms.append_journal(
            "qa", (q or "(无标题提问)")[:200],
            ts=ts,
            session=c.get("session_id", "-"),
            answer=a[:600] or "-",
            source="card_archive")
        n += 1
    print(f"[evolve] 回填 {n} 条历史问答到 journal")
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印，不落盘不推送")
    ap.add_argument("--no-push", action="store_true", help="落盘但不推审批卡")
    ap.add_argument("--json", action="store_true", help="输出原始 JSON")
    ap.add_argument("--backfill-cards", action="store_true",
                    help="把 card_archive.json 的历史问答回填进 journal 后退出")
    args = ap.parse_args()

    if args.backfill_cards:
        backfill_cards()
        return 0

    ev = collect_evidence()
    if not ev["journal"] and not ev["recent_qa"]:
        print("[evolve] 没有任何证据，跳过（等积累够再跑）")
        return 0

    prompt = build_prompt(ev)
    print(f"[evolve] 证据：journal {len(ev['journal'])} 条 / 问答 {len(ev['recent_qa'])} 条")
    try:
        raw, model = call_llm(prompt)
    except Exception as e:  # noqa: BLE001
        print(f"[evolve] LLM 调用失败: {e}", file=sys.stderr)
        return 1

    props = parse_proposals(raw)
    if args.json:
        print(json.dumps(props, ensure_ascii=False, indent=2))
    else:
        print(f"[evolve] {model} 提出 {len(props)} 条提案")
    if not props:
        ms.append_journal("evolve", "本次扫描没有值得提的提案",
                          journal_entries=len(ev["journal"]))
        # 就算这轮没新提案，上一轮没批的（"稍后再说"→ 仍是 pending）也该再问一次
        if not args.no_push:
            _push_cards([])
        return 0

    if args.dry_run:
        print(json.dumps(props, ensure_ascii=False, indent=2))
        return 0

    saved: list[str] = []
    batch_items: list[tuple[str, dict]] = []
    skipped = 0
    for p in props:
        # ---- 去重闸门（确定性，在推卡之前就拦掉）----
        # 同一个坑提两次会生成两条记忆，这是记忆库退化的主因。
        scope = p.get("scope", "global")
        sup = p.get("supersedes", "")
        seen, why = ms.evidence_seen(p.get("evidence", ""))
        if seen and not sup:
            print(f"[evolve] 跳过（{why}）")
            ms.append_journal("evolve_skip", f"跳过重复提案: {p.get('title', '')[:60]}",
                              reason=why)
            skipped += 1
            continue
        if not sup:
            probe = f"{p.get('title', '')} {p.get('body', '')}"
            near = [t for t in ms.find_similar(probe, threshold=ms.DUP_THRESHOLD, limit=2)
                    if t[1]["scope"] == scope]
            if near:
                s, e = near[0]
                print(f"[evolve] 跳过（与 [{e['memoryId']}] {s:.2f} 重合）")
                ms.append_journal("evolve_skip", f"跳过近重复提案: {p.get('title', '')[:60]}",
                                  similar=e["memoryId"], score=f"{s:.2f}")
                skipped += 1
                continue
        # 上限预检：满了就别推卡了，推了也批不了
        actives = ms.count_active(scope)
        if not sup and actives >= ms.MAX_LEARNED_RULES:
            print(f"[evolve] 跳过（{scope} 已达上限 {ms.MAX_LEARNED_RULES}）")
            ms.append_journal("evolve_skip", f"跳过提案（已达上限）: {p.get('title', '')[:60]}",
                              scope=scope, active=actives)
            skipped += 1
            continue

        pid = ms.save_proposal({
            "type": p.get("type", "memory"),
            "title": p.get("title", ""),
            "scope": scope,
            "category": p.get("category", "pattern"),
            "confidence": p.get("confidence", "medium"),
            "body": p.get("body", ""),
            "evidence": p.get("evidence", ""),
            "supersedes": sup,
            "risk": p.get("risk", ""),
            "expected_gain": p.get("expected_gain", ""),
            "source": "evolve",
            "proposed_by": model,      # 哪个模型提的（provenance）
        })
        p["id"] = pid
        saved.append(pid)
        print(f"[evolve] 提案已存 {pid}: {p.get('title', '')[:50]}")
        batch_items.append((pid, p))

    # 一次性推**一张**批量审批卡（2026-10-22 改：原来一条提案一张卡）。
    # 超过 4 条就分批推（表单域太多手机上不好点），每批仍是一次提交结算一批。
    if batch_items and not args.no_push:
        _push_cards(batch_items)

    if saved:
        ms.append_journal("evolve", f"扫描完成，提出 {len(saved)} 条提案，等待用户审批",
                          proposals=",".join(saved), skipped=skipped)
    else:
        ms.append_journal("evolve", f"扫描完成，{len(props)} 条全部被去重闸门跳过",
                          skipped=skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
