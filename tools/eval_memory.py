"""eval_memory —— 记忆/对话质量的**确定性**指标快照。

定位：它是**评估器**，住在改进循环之外（tools/evolve.py 的提案者看不到它，
也改不了它）。按研究里的「验证层级」，能用形式化/确定性检查的，就不要用
LLM 打分 —— 后者是最容易被 hack 的一档。

只做可复现的统计，不做主观判断：
- 问答量、回答长度分布、问题关键词命中率
- 用户反馈信号（retry / stop / like）—— 最硬的负反馈
- 记忆库健康度：条数、分类分布、置信度分布、门禁拦截次数
- 提案闭环：提出 / 批准 / 驳回 / 暂缓，以及**批准率**

输出：
    python tools/eval_memory.py              # 打印表格
    python tools/eval_memory.py --json       # 机器可读
    python tools/eval_memory.py --save       # 存到 ~/.dingtalk-bot/logs/eval/<date>.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools import memory_store as ms  # noqa: E402

EVAL_HOME = os.path.join(os.path.expanduser("~"), ".dingtalk-bot", "logs", "eval")

# 中文没有词边界，关键词命中用「子串包含」而不是分词
STOPWORDS = set("的 了 和 是 在 我 你 有 就 不 人 都 一 一个 上 也 很 到 说 要 去 会 着 没 看 好 自己 这".split())


def qa_stats() -> dict:
    arc = os.path.join(REPO, "bot", "bridge_data", "card_archive.json")
    if not os.path.exists(arc):
        return {"count": 0}
    try:
        data = json.load(open(arc, encoding="utf-8"))
    except Exception:
        return {"count": 0}
    cards = list((data.get("cards") or {}).values())
    qlens, alens, hits, total_kw = [], [], 0, 0
    empty_q = 0
    for c in cards:
        q = (c.get("question") or "").strip()
        a = (c.get("answer") or "").strip()
        if not q:
            empty_q += 1
            continue
        qlens.append(len(q))
        alens.append(len(a))
        kws = [k for k in re.split(r"[\s，。、？！,.?!；;：:]+", q) if len(k) >= 2 and k not in STOPWORDS]
        if kws:
            total_kw += 1
            if any(k in a for k in kws):
                hits += 1
    out = {"count": len(cards), "empty_question": empty_q}
    if qlens:
        out["q_len_median"] = int(statistics.median(qlens))
        out["a_len_median"] = int(statistics.median(alens))
        out["a_len_min"] = min(alens)
        out["a_len_max"] = max(alens)
    if total_kw:
        out["keyword_hit_rate"] = round(hits / total_kw, 3)
    return out


def feedback_stats() -> dict:
    """用户反馈信号：retry / stop 是负反馈，like 是正反馈。"""
    counts = {}
    for r in ms.read_journal(2000):
        k = (r.get("kind") or "").split(" ")[0].strip()
        if k in ("retry", "stop", "like", "qa", "evolve", "proposal_approved",
                "proposal_rejected", "proposal_deferred"):
            counts[k] = counts.get(k, 0) + 1
    neg = counts.get("retry", 0) + counts.get("stop", 0)
    pos = counts.get("like", 0)
    total = neg + pos
    out = dict(counts)
    out["negative"] = neg
    out["positive"] = pos
    out["satisfaction_rate"] = round(pos / total, 3) if total else None
    return out


def memory_stats() -> dict:
    entries = ms.all_active_entries()
    by_cat, by_conf, by_scope, by_trust = {}, {}, {}, {}
    for e in entries:
        by_cat[e["category"]] = by_cat.get(e["category"], 0) + 1
        by_conf[e["confidence"]] = by_conf.get(e["confidence"], 0) + 1
        by_scope[e["scope"]] = by_scope.get(e["scope"], 0) + 1
        by_trust[e["trust"]] = by_trust.get(e["trust"], 0) + 1
    total_all = len(ms.all_entries())
    return {
        "total": len(entries),
        "total_including_superseded": total_all,
        "superseded": total_all - len(entries),
        "cap": ms.MAX_LEARNED_RULES,
        "by_category": by_cat,
        "by_confidence": by_conf,
        "by_scope": by_scope,
        "by_trust": by_trust,
        "pinned": len([e for e in entries if e.get("immutable")]),
        "journal_entries": len(ms.read_journal(5000)),
    }


def acceptance_stats() -> dict:
    """落地验收趋势：每次提案落地前后的记忆库快照。"""
    recs = ms.read_acceptance(500)
    if not recs:
        return {"count": 0}
    deltas = []
    for r in recs:
        b, a = r.get("before") or {}, r.get("after") or {}
        if "active" in b and "active" in a:
            deltas.append(a["active"] - b["active"])
    return {
        "count": len(recs),
        "net_growth": sum(deltas),
        "replacements": len([r for r in recs if r.get("supersedes")]),
        "last": recs[-1] if recs else None,
    }


def proposal_stats() -> dict:
    pend = ms.list_proposals("pending")
    arch_dir = os.path.join(ms.PROPOSAL_DIR, "archive")
    approved = rejected = deferred = supersede = 0
    if os.path.isdir(arch_dir):
        for n in os.listdir(arch_dir):
            if not n.endswith(".json"):
                continue
            try:
                p = json.load(open(os.path.join(arch_dir, n), encoding="utf-8"))
            except Exception:
                continue
            if p.get("status") == "approved":
                approved += 1
                if p.get("supersedes"):
                    supersede += 1
    if os.path.isdir(ms.REJECTED_DIR):
        rejected = len([n for n in os.listdir(ms.REJECTED_DIR) if n.endswith(".json")])
    decided = approved + rejected
    return {
        "pending": len(pend),
        "approved": approved,
        "rejected": rejected,
        "deferred": deferred,
        "supersede_approved": supersede,
        "acceptance_rate": round(approved / decided, 3) if decided else None,
    }


def snapshot() -> dict:
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "qa": qa_stats(),
        "feedback": feedback_stats(),
        "memory": memory_stats(),
        "proposals": proposal_stats(),
        "acceptance": acceptance_stats(),
    }


def render(s: dict) -> str:
    L = []
    L.append("=" * 52)
    L.append(f"记忆/对话质量快照  {s['ts']}")
    L.append("=" * 52)
    qa = s["qa"]
    L.append("[问答]")
    L.append(f"  卡片总数        {qa.get('count', 0)}")
    if "a_len_median" in qa:
        L.append(f"  回答长度中位数  {qa['a_len_median']}  (min {qa['a_len_min']} / max {qa['a_len_max']})")
    if "keyword_hit_rate" in qa:
        L.append(f"  问题关键词命中  {qa['keyword_hit_rate']}")
    if qa.get("empty_question"):
        L.append(f"  无标题提问      {qa['empty_question']}")
    fb = s["feedback"]
    L.append("[用户反馈]")
    L.append(f"  正反馈 {fb.get('positive', 0)} / 负反馈 {fb.get('negative', 0)}"
             f"  (retry={fb.get('retry', 0)} stop={fb.get('stop', 0)})")
    if fb.get("satisfaction_rate") is not None:
        L.append(f"  满意度          {fb['satisfaction_rate']}")
    mem = s["memory"]
    L.append("[记忆库]")
    L.append(f"  活动 {mem['total']}/{mem.get('cap', '-')}   pinned {mem.get('pinned', 0)}"
             f"   已取代 {mem.get('superseded', 0)}   journal {mem['journal_entries']} 行")
    L.append(f"  分类 {mem['by_category']}")
    L.append(f"  置信 {mem['by_confidence']}   信任 {mem['by_trust']}")
    pr = s["proposals"]
    L.append("[提案闭环]")
    L.append(f"  待审 {pr['pending']}  已批 {pr['approved']}  已驳 {pr['rejected']}"
             f"  其中取代型 {pr.get('supersede_approved', 0)}")
    if pr.get("acceptance_rate") is not None:
        L.append(f"  批准率          {pr['acceptance_rate']}")
    acc = s.get("acceptance") or {}
    if acc.get("count"):
        L.append("[落地验收]")
        L.append(f"  已记录 {acc['count']} 次落地  净增 {acc['net_growth']:+d} 条"
                 f"  取代型 {acc.get('replacements', 0)}")
        last = acc.get("last") or {}
        if last:
            b, a = last.get("before") or {}, last.get("after") or {}
            L.append(f"  最近: {last.get('title', '')[:40]}")
            L.append(f"        活动 {b.get('active', '-')}→{a.get('active', '-')}"
                     f"  覆盖 {b.get('coverage', '-')}→{a.get('coverage', '-')}")
    L.append("=" * 52)
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--save", action="store_true", help="存到 ~/.dingtalk-bot/logs/eval/")
    args = ap.parse_args()
    s = snapshot()
    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
    else:
        print(render(s))
    if args.save:
        os.makedirs(EVAL_HOME, exist_ok=True)
        path = os.path.join(EVAL_HOME, f"eval-{time.strftime('%Y%m%d-%H%M%S')}.json")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(s, fh, ensure_ascii=False, indent=2)
        print(f"saved: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
