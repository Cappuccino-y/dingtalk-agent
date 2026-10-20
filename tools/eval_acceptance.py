"""eval_acceptance —— 自我改进的**验收**评估器（住在 loop 外）。

它回答一个别的东西回答不了的问题：**这次改动到底有没有让回答变好？**

和 eval_memory.py 的分工：
    eval_memory.py      健康度 / 规模 / 提案批准率 —— 便宜，看趋势
    eval_acceptance.py  变更验收 —— 有 LLM 生成、但**打分完全确定性**

## 双切分（double split）

`card_archive.json` 的 30 条真实问答按时间**交替切分**：

    held-out（judgment）  偶数位 —— **永远不给提案器看**，只用来验收
    held-in（evidence）   奇数位 —— 提案器可以引用

不切开的话，提案器会拿「自己见过的例子」当证据，验收就是自证。
交替切分是简化做法（真正干净的做法是攒一个独立冻结集），但至少不重叠。

## 验收怎么做

对每条 held-out 问题：

1. 找和它最相关的记忆条目（确定性：字符二元组相似度）
2. 没有相关条目 → 跳过（没有记忆可用，判不了）
3. 让 LLM 答两遍：**不给记忆** / **给记忆**
4. 打分（确定性）：答案里出现了多少「相关记忆独有的二元组」
   —— 出现得多，说明记忆真的被用上了，而不是模型自己编

验收结论 = 有记忆的那一遍，命中率的平均提升。提升 ≤ 0 就是没效果。

用法：
    python tools/eval_acceptance.py                  # 健康度 + 切分信息
    python tools/eval_acceptance.py --replay 6       # 真跑 A/B（6 条，约 1 分钟）
    python tools/eval_acceptance.py --proposal <id>  # 单条提案的预审
    python tools/eval_acceptance.py --json
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

ARCHIVE = os.path.join(REPO, "bot", "bridge_data", "card_archive.json")
SCOPES = ("global", "project:dingtalk-agent")

# 相关记忆的门槛。低于它就认为「这条问题没有对应的记忆」，跳过不判。
RELEVANT_THRESHOLD = 0.12


# --------------------------------------------------------------- 切分
def _load_cards() -> list[dict]:
    if not os.path.exists(ARCHIVE):
        return []
    try:
        data = json.load(open(ARCHIVE, encoding="utf-8"))
    except Exception:
        return []
    items = [c for c in (data.get("cards") or {}).values()
             if (c.get("question") or "").strip()]
    items.sort(key=lambda c: c.get("at", 0))
    return items


def split_cards() -> tuple[list[dict], list[dict]]:
    """返回 (held_out, held_in)。按时间交替切，保证两者不重叠。"""
    items = _load_cards()
    return items[::2], items[1::2]


def judgment_cards() -> list[dict]:
    return split_cards()[0]


def evidence_cards(limit: int = 20) -> list[dict]:
    """提案器能看的那一半（held-in）。evolve.py 用它，避免污染验收集。"""
    return split_cards()[1][-limit:]


def heldout_fingerprints() -> set[str]:
    """held-out 判例的指纹（ts|question）。

    journal 里回填过的历史问答包含全部 30 条，提案器如果原样读 journal，
    held-out 就等于泄漏给它了。用这个集合在证据侧过滤掉。
    """
    out = set()
    for c in judgment_cards():
        at = c.get("at")
        ts = ""
        if isinstance(at, (int, float)) and at > 0:
            ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(at))
        out.add(f"{ts}|{(c.get('question') or '').strip()[:200]}")
    return out


# --------------------------------------------------------------- 确定性指标
def _bigrams(s: str) -> set:
    s = re.sub(r"\s+", "", s or "")
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _memory_blob(scope: str) -> str:
    return " ".join(f"{e['title']} {e['body']}" for e in ms.active_entries(scope))


def coverage(scope: str = "global") -> float:
    """held-out 问题被记忆覆盖的程度（健康指标，**不是**验收结论）。

    只说明「记忆库聊到了多少用户关心的话题」，不说明回答质量。
    """
    cards = judgment_cards()
    if not cards:
        return 0.0
    blob = _bigrams(_memory_blob(scope))
    if not blob:
        return 0.0
    scores = []
    for c in cards:
        q = _bigrams(c.get("question", ""))
        if q:
            scores.append(len(q & blob) / len(q))
    return round(sum(scores) / len(scores), 4) if scores else 0.0


def health() -> dict:
    """记忆库健康度（全确定性，不调 LLM）。"""
    out = {"cards_heldout": len(judgment_cards()),
           "cards_heldin": len(split_cards()[1]), "scopes": {}}
    for sc in SCOPES:
        act = ms.active_entries(sc)
        all_e = ms.read_entries(sc)
        # 重复簇：互相关于 DUP_THRESHOLD 的条目（说明该合并了）
        dups = 0
        for i, a in enumerate(act):
            for b in act[i + 1:]:
                if ms.similarity(f"{a['title']} {a['body']}",
                                 f"{b['title']} {b['body']}") >= ms.DUP_THRESHOLD:
                    dups += 1
        out["scopes"][sc] = {
            "active": len(act),
            "cap": ms.MAX_LEARNED_RULES,
            "superseded": len([e for e in all_e if e.get("superseded_by")]),
            "pinned": len([e for e in act if e.get("immutable")]),
            "avg_title_chars": round(sum(len(e["title"]) for e in act) / len(act), 1) if act else 0,
            "avg_body_chars": round(sum(len(e["body"]) for e in act) / len(act), 1) if act else 0,
            "duplicate_pairs": dups,
            "coverage": coverage(sc),
        }
    return out


# --------------------------------------------------------------- 真 A/B
def _judge_system() -> str:
    return ("你是用户的私人助理。回答要直接、具体，用中文，不要客套。"
            "如果给了【已知约定】就按它来做；没给就按你的常识回答。")


def _keywords(entry: dict) -> set:
    """一条记忆「独有」的二元组（标题为主，标题区分度高）。"""
    return _bigrams(entry["title"])


def _score(answer: str, mem_terms: set) -> float:
    """答案用上了多少目标记忆的独有二元组。确定性，不看语义、不调模型。"""
    if not mem_terms:
        return 0.0
    a = _bigrams(answer)
    return len(a & mem_terms) / len(mem_terms)


def replay(limit: int = 6, verbose: bool = True) -> dict:
    """真跑 A/B：同一批 held-out 问题，给记忆 vs 不给记忆，比较记忆使用率。"""
    from tools.llm_client import OpenAICompatibleClient

    cards = judgment_cards()[:limit]
    if not cards:
        return {"error": "没有 held-out 判例（card_archive.json 缺失或为空）"}

    blob = "\n\n".join(ms.brief(sc, limit=60) for sc in SCOPES)
    client = OpenAICompatibleClient.for_job("EVOLVE")

    rows = []
    for c in cards:
        q = (c.get("question") or "").strip()
        hits = [(s, e) for s, e in ms.find_similar(q, threshold=RELEVANT_THRESHOLD, limit=2)
                if e["scope"] in SCOPES]
        if not hits:
            continue                      # 没有相关记忆 -> 判不了，跳过
        terms = set()
        for _, e in hits:
            terms |= _keywords(e)

        base_prompt = f"用户问：{q}"
        with_prompt = (f"【已知约定】\n{blob}\n\n"
                       f"（与本次相关的是：{'; '.join(e['title'] for _, e in hits)}）\n\n"
                       f"用户问：{q}")
        try:
            ans_wo = client.generate(base_prompt, system_prompt=_judge_system())
            ans_wi = client.generate(with_prompt, system_prompt=_judge_system())
        except Exception as ex:  # noqa: BLE001
            return {"error": f"LLM 调用失败: {ex}", "rows": rows}

        s_wo, s_wi = _score(ans_wo, terms), _score(ans_wi, terms)
        rows.append({
            "q": q[:80],
            "related": [e["title"][:40] for _, e in hits],
            "score_without": round(s_wo, 3),
            "score_with": round(s_wi, 3),
            "delta": round(s_wi - s_wo, 3),
            "len_without": len(ans_wo),
            "len_with": len(ans_wi),
        })
        if verbose:
            print(f"  [{s_wo:.2f} -> {s_wi:.2f}] {q[:50]}")

    judged = len(rows)
    if not judged:
        return {"judged": 0, "note": "所有 held-out 问题都没有相关记忆，无法验收"}
    avg_delta = sum(r["delta"] for r in rows) / judged
    improved = len([r for r in rows if r["delta"] > 0.001])
    return {
        "judged": judged,
        "avg_delta": round(avg_delta, 4),
        "improved": improved,
        "regressed": len([r for r in rows if r["delta"] < -0.001]),
        "neutral": judged - improved - len([r for r in rows if r["delta"] < -0.001]),
        "verdict": ("记忆有效" if avg_delta > 0.02
                    else "记忆基本没影响（需要更好的条目）" if avg_delta >= -0.02
                    else "记忆在帮倒忙（负迁移）"),
        "rows": rows,
    }


# --------------------------------------------------------------- 单条提案预审
def preflight(pid: str) -> dict:
    """批准前的预审：会不会被去重闸门拦、会不会超上限、有没有相近条目。"""
    p = ms.load_proposal(pid)
    if not p:
        return {"error": f"提案不存在: {pid}"}
    scope = p.get("scope", "global")
    seen, why = ms.evidence_seen(p.get("evidence", ""))
    probe = f"{p.get('title', '')} {p.get('body', '')}"
    near = ms.find_similar(probe, threshold=0.0, limit=3)
    return {
        "id": pid,
        "title": p.get("title", ""),
        "scope": scope,
        "would_skip": seen,
        "skip_reason": why,
        "active": ms.count_active(scope),
        "cap": ms.MAX_LEARNED_RULES,
        "slots_left": ms.MAX_LEARNED_RULES - ms.count_active(scope),
        "similar": [{"id": e["memoryId"], "title": e["title"], "score": round(s, 3)}
                    for s, e in near],
        "verdict": ("会被去重闸门拦下" if seen
                    else "已达上限，需要先合并/retire" if ms.count_active(scope) >= ms.MAX_LEARNED_RULES
                    else "可以落地"),
    }


def render(h: dict) -> str:
    L = ["=" * 56, "验收评估器（eval_acceptance）", "=" * 56]
    L.append(f"判例切分：held-out {h['cards_heldout']} 条 / held-in {h['cards_heldin']} 条")
    for sc, s in h["scopes"].items():
        L.append(f"\n[{sc}]")
        L.append(f"  活动 {s['active']}/{s['cap']}   已取代 {s['superseded']}   pinned {s['pinned']}")
        L.append(f"  标题均长 {s['avg_title_chars']}  正文均长 {s['avg_body_chars']}")
        L.append(f"  重复簇 {s['duplicate_pairs']}   覆盖度 {s['coverage']}")
    L.append("\n注：coverage 是健康指标，不是验收结论。验收要跑 --replay。")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--replay", type=int, default=0, metavar="N", help="真跑 A/B，N 条判例")
    ap.add_argument("--proposal", default="", help="预审一条提案")
    args = ap.parse_args()

    if args.proposal:
        r = preflight(args.proposal)
        print(json.dumps(r, ensure_ascii=False, indent=2) if args.json else
              f"{r.get('verdict', r.get('error'))}\n" +
              "\n".join(f"  {k}: {v}" for k, v in r.items() if k != "verdict"))
        return 0

    if args.replay:
        print(f"跑 A/B（{args.replay} 条 held-out）...")
        r = replay(args.replay)
        if args.json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
        else:
            if r.get("error"):
                print("ERR " + r["error"])
            else:
                print(f"\n判了 {r['judged']} 条：提升 {r['improved']} / "
                      f"退化 {r['regressed']} / 无变化 {r['neutral']}")
                print(f"平均提升 Δ={r['avg_delta']:+.3f}  →  {r['verdict']}")
        return 0

    h = health()
    print(json.dumps(h, ensure_ascii=False, indent=2) if args.json else render(h))
    return 0


if __name__ == "__main__":
    sys.exit(main())
