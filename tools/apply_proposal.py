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


def approve(pid: str) -> tuple[bool, str]:
    p = ms.load_proposal(pid)
    if not p:
        return False, f"提案不存在: {pid}"
    if p.get("status") != "pending":
        return False, f"提案状态不是 pending: {p.get('status')}"

    scope = p.get("scope", "global")
    if scope not in ALLOWED_SCOPES:
        return False, f"目标不在白名单: {scope}（profile.md 是手写层，只能人工改）"

    if p.get("type") == "convention":
        # 约定类：先落成一条 memory（category=preference），profile.md 由用户手工搬
        entry = ms.Entry(
            scope=scope, category="preference", title=p.get("title", ""),
            body=p.get("body", ""), tags=["convention", "approved"],
            confidence=p.get("confidence", "medium"), trust="self",
            source_agent_ids=["evolve", "user-approved"])
    else:
        entry = ms.Entry(
            scope=scope, category=p.get("category", "pattern"),
            title=p.get("title", ""), body=p.get("body", ""),
            tags=["evolve"], confidence=p.get("confidence", "medium"),
            trust="self", source_agent_ids=["evolve", "user-approved"])

    try:
        mid = ms.append_entry(entry)
    except ms.WriteRejected as e:
        return False, f"写入门禁拒绝: {e}"

    ms.archive_proposal(pid, "approved")
    ms.append_journal("proposal_approved",
                      f"提案已落地: {p.get('title', '')}",
                      proposal=pid, memory_id=mid, scope=scope)

    # 只提交**这条提案动过的文件** —— 不能 git add -A：那会把工作区里所有
    # 未提交的改动（别人的活、别的分支的实验）一起吞进这个 commit。
    # 提案落地的 diff 必须是干净、可单独 revert 的。
    rel_mem = os.path.relpath(ms.scope_path(scope), REPO).replace("\\", "/")
    rel_prop = os.path.relpath(os.path.join(ms.PROPOSAL_DIR, "archive",
                                            f"{pid}.json"), REPO).replace("\\", "/")
    paths = [p_ for p_ in (rel_mem, rel_prop) if os.path.exists(os.path.join(REPO, p_))]
    try:
        git("add", "--", *paths)
        msg = f"learn(proposal): {p.get('title', '')[:60]} [pid={pid} mid={mid}]"
        git("-c", "user.name=opencode", "-c", "user.email=opencode@local",
            "commit", "-m", msg, "--", *paths)
        sha = git("rev-parse", "--short", "HEAD").stdout.strip()
    except subprocess.CalledProcessError as e:
        return True, f"已写入 {mid}（git commit 失败: {e.stderr[:200]}）"

    return True, f"已落地 {mid} -> {scope} (commit {sha}, {len(paths)} 个文件)"


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
    ap.add_argument("--note", default="")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list or not args.proposal_id:
        pend = ms.list_proposals("pending")
        if not pend:
            print("(没有待审批的提案)")
            return 0
        for p in pend:
            print(f"- {p['id']}  [{p.get('type')}] {p.get('title')}")
            print(f"    scope={p.get('scope')} category={p.get('category')} "
                  f"confidence={p.get('confidence')}")
            print(f"    依据: {p.get('evidence', '-')}")
            print(f"    风险: {p.get('risk', '-')}")
        return 0

    if args.approve:
        ok, msg = approve(args.proposal_id)
    elif args.reject:
        ok, msg = reject(args.proposal_id, args.note)
    elif args.defer:
        ok, msg = defer(args.proposal_id)
    else:
        print("需要 --approve / --reject / --defer 之一")
        return 2

    print(("OK  " if ok else "ERR ") + msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
