"""apply_proposal —— 落地/驳回一条改进提案。**确定性代码，不经 LLM。**

隔离要点（Outside-the-Loop Invariant）：
- 提案由 LLM 提出（tools/evolve.py，直连 API，无工具），落地由**本文件**完成。
- 本文件只允许改白名单里的目标，且必须通过 memory_store 的写入门禁。
- 每次落地 = 一次 git commit，可 `git revert`。
- profile.md 是**手写层**，不在自动落地白名单里 —— 提案可以建议改它，但只能人工改。

用法：
    python tools/apply_proposal.py <proposal_id> --approve
    python tools/apply_proposal.py <proposal_id> --reject [--note "理由"]
    python tools/apply_proposal.py --list
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools import memory_store as ms  # noqa: E402

# 自动落地的白名单。profile.md 故意不在里面（手写层，必须人工改）。
ALLOWED_SCOPES = ("global", "project:dingtalk-agent")


def git(*args, check=True):
    return subprocess.run(["git", "-C", REPO, *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          check=check)


def _snapshot(scope: str) -> dict:
    """记忆库快照（落地前后各取一次，写进 acceptance.jsonl）。

    只算确定性指标。覆盖率算不出来就省略，绝不编数。
    """
    act = ms.active_entries(scope)
    snap = {
        "active": len(act),
        "chars": sum(len(e.get("body", "")) + len(e.get("title", "")) for e in act),
        "superseded": len([e for e in ms.read_entries(scope) if e.get("superseded_by")]),
        "pinned": len([e for e in act if e.get("immutable")]),
    }
    try:
        from tools import eval_acceptance
        snap["coverage"] = eval_acceptance.coverage(scope)
    except Exception:
        pass
    return snap


def approve(pid: str, force: bool = False) -> tuple[bool, str]:
    p = ms.load_proposal(pid)
    if not p:
        return False, f"提案不存在: {pid}"
    if p.get("status") != "pending":
        return False, f"提案状态不是 pending: {p.get('status')}"

    scope = p.get("scope", "global")
    if scope not in ALLOWED_SCOPES:
        return False, f"目标不在白名单: {scope}（profile.md 是手写层，只能人工改）"

    supersedes = (p.get("supersedes") or "").strip()

    # ---- 去重闸门（确定性，不经 LLM）----
    # 已经学过的不要再学一遍：记忆库单调重复是自我改进系统最常见的退化方式。
    if not supersedes and not force:
        seen, why = ms.evidence_seen(p.get("evidence", ""))
        if seen:
            return False, f"跳过（证据已处理过）: {why}。确认要重复记录请用 --force"
        probe = f"{p.get('title', '')} {p.get('body', '')}"
        near = [(s, e) for s, e in ms.find_similar(probe, threshold=ms.DUP_THRESHOLD, limit=3)
                if e["scope"] == scope]
        if near:
            s, e = near[0]
            return False, (f"与已存条目高度重合（{s:.2f}）：[{e['memoryId']}] {e['title']}。"
                           f"要改写它请用 supersedes={e['memoryId']}，坚持新增请用 --force")

    # ---- provenance：把用户原话回溯到真实证据 id ----
    refs = ms.match_evidence(p.get("evidence", ""))
    src_msg = refs + [f"proposal:{pid}"]

    tags = ["convention", "approved"] if p.get("type") == "convention" else ["evolve"]
    entry = ms.Entry(
        scope=scope,
        category=p.get("category", "pattern") if p.get("type") != "convention" else "preference",
        title=p.get("title", ""),
        body=p.get("body", ""),
        tags=tags,
        confidence=p.get("confidence", "medium"),
        trust="self",
        source_message_ids=src_msg,
        source_agent_ids=["evolve", "user-approved"],
        supersedes=supersedes,
    )

    before = _snapshot(scope)
    try:
        mid = ms.append_entry(entry)
    except ms.WriteRejected as e:
        return False, f"写入门禁拒绝: {e}"
    after = _snapshot(scope)

    ms.archive_proposal(pid, "approved")
    ms.append_journal("proposal_approved",
                      f"提案已落地: {p.get('title', '')}",
                      proposal=pid, memory_id=mid, scope=scope,
                      supersedes=supersedes or "-",
                      evidence_refs=", ".join(refs) or "-")
    ms.append_acceptance({"pid": pid, "mid": mid, "scope": scope,
                          "title": p.get("title", ""),
                          "supersedes": supersedes or "",
                          "before": before, "after": after})

    # 只提交**这条提案动过的文件** —— 不能 git add -A：那会把工作区里所有
    # 未提交的改动（别人的活、别的分支的实验）一起吞进这个 commit。
    # 提案落地的 diff 必须是干净、可单独 revert 的。
    rel_mem = os.path.relpath(ms.scope_path(scope), REPO).replace("\\", "/")
    rel_prop = os.path.relpath(os.path.join(ms.PROPOSAL_DIR, "archive",
                                            f"{pid}.json"), REPO).replace("\\", "/")
    rel_acc = os.path.relpath(os.path.join(ms.MEMORY_DIR, "acceptance.jsonl"),
                             REPO).replace("\\", "/")
    paths = [p_ for p_ in (rel_mem, rel_prop, rel_acc)
             if os.path.exists(os.path.join(REPO, p_))]
    try:
        git("add", "--", *paths)
        msg = f"learn(proposal): {p.get('title', '')[:60]} [pid={pid} mid={mid}]"
        if supersedes:
            msg += f"\nsupersedes: {supersedes}"
        git("-c", "user.name=opencode", "-c", "user.email=opencode@local",
            "commit", "-m", msg, "--", *paths)
        sha = git("rev-parse", "--short", "HEAD").stdout.strip()
    except subprocess.CalledProcessError as e:
        return True, f"已写入 {mid}（git commit 失败: {e.stderr[:200]}）"

    tail = f"，取代 {supersedes}" if supersedes else ""
    return True, (f"已落地 {mid} -> {scope}{tail} "
                  f"(活动 {before['active']}→{after['active']}, commit {sha})")


def retire(mid: str, note: str = "", scope: str = "global") -> tuple[bool, str]:
    """显式废弃一条记忆（append-only 的「删除」）。

    不是物理删除：追加一条 tombstone 条目来取代它。这样历史可追溯，
    `git revert` 也能撤销。IMMUTABLE（pinned）条目拒绝废弃。
    """
    act = ms.active_entries(scope)
    tgt = next((e for e in act if e["memoryId"] == mid), None)
    if not tgt:
        return False, f"活动条目里找不到 {mid}（scope={scope}）"
    if tgt.get("immutable"):
        return False, f"{mid} 是 IMMUTABLE（pinned），不能废弃。先手动去掉 pinned 标签"

    entry = ms.Entry(
        scope=scope, category="decision",
        title=f"已废弃: {tgt['title'][:50]}",
        body=f"废弃条目: {mid}\n原因: {note or '(未注明)'}\n原内容不再作为行为依据。",
        tags=["tombstone"], confidence="high", trust="self",
        source_message_ids=[f"retire:{mid}"],
        source_agent_ids=["user-retire"],
        supersedes=mid,
    )
    try:
        tomb = ms.append_entry(entry)
    except ms.WriteRejected as e:
        return False, f"写入门禁拒绝: {e}"

    ms.append_journal("memory_retired", f"已废弃 {mid}: {tgt['title'][:60]}",
                      retired=mid, tombstone=tomb, note=note or "-")
    rel_mem = os.path.relpath(ms.scope_path(scope), REPO).replace("\\", "/")
    try:
        git("add", "--", rel_mem)
        git("-c", "user.name=opencode", "-c", "user.email=opencode@local",
            "commit", "-m", f"memory(retire): {tgt['title'][:60]} [mid={mid}]",
            "--", rel_mem)
        sha = git("rev-parse", "--short", "HEAD").stdout.strip()
    except subprocess.CalledProcessError as e:
        return True, f"已写入 tombstone {tomb}（git commit 失败: {e.stderr[:200]}）"
    return True, f"已废弃 {mid}，tombstone={tomb} (commit {sha})"


def reject(pid: str, note: str = "") -> tuple[bool, str]:
    p = ms.load_proposal(pid)
    if not p:
        return False, f"提案不存在: {pid}"
    ms.archive_proposal(pid, "rejected", note=note)
    ms.append_journal("proposal_rejected",
                      f"提案被驳回: {p.get('title', '')}",
                      proposal=pid, note=note or "-")
    return True, "已驳回并归档（下次提议会避开）"


def defer(pid: str) -> tuple[bool, str]:
    p = ms.load_proposal(pid)
    if not p:
        return False, f"提案不存在: {pid}"
    p["status"] = "deferred"
    p["deferred_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(ms.proposal_path(pid), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(p, fh, ensure_ascii=False, indent=2)
    ms.append_journal("proposal_deferred", f"提案暂缓: {p.get('title', '')}",
                      proposal=pid)
    return True, "已标记为暂缓（状态不变，仍是 pending）"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("proposal_id", nargs="?")
    ap.add_argument("--approve", action="store_true")
    ap.add_argument("--reject", action="store_true")
    ap.add_argument("--defer", action="store_true")
    ap.add_argument("--retire", metavar="MEMORY_ID", default="",
                    help="废弃一条记忆（append tombstone，非物理删除）")
    ap.add_argument("--scope", default="global", help="配合 --retire 使用")
    ap.add_argument("--force", action="store_true", help="跳过去重闸门（明知重复仍要记录）")
    ap.add_argument("--note", default="")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.retire:
        ok, msg = retire(args.retire, note=args.note, scope=args.scope)
        print(("OK  " if ok else "ERR ") + msg)
        return 0 if ok else 1

    if args.list or not args.proposal_id:
        pend = ms.list_proposals("pending")
        print("=== 记忆库上限 ===")
        for sc in ALLOWED_SCOPES:
            print(f"  {sc}: 活动 {ms.count_active(sc)}/{ms.MAX_LEARNED_RULES}")
        if not pend:
            print("\n(没有待审批的提案)")
            return 0
        print("\n=== 待审批 ===")
        for p in pend:
            print(f"- {p['id']}  [{p.get('type')}] {p.get('title')}")
            print(f"    scope={p.get('scope')} category={p.get('category')} "
                  f"confidence={p.get('confidence')}"
                  + (f" supersedes={p.get('supersedes')}" if p.get("supersedes") else ""))
            print(f"    依据: {p.get('evidence', '-')}")
            print(f"    风险: {p.get('risk', '-')}")
            # 预演去重闸门：让用户点批准前就知道会不会被拦
            seen, why = ms.evidence_seen(p.get("evidence", ""))
            if seen:
                print(f"    ⚠️ 去重: {why}")
        return 0

    if args.approve:
        ok, msg = approve(args.proposal_id, force=args.force)
    elif args.reject:
        ok, msg = reject(args.proposal_id, args.note)
    elif args.defer:
        ok, msg = defer(args.proposal_id)
    else:
        print("需要 --approve / --reject / --defer / --retire 之一")
        return 2

    print(("OK  " if ok else "ERR ") + msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
