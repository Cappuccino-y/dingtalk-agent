"""
审批回调「重复投递」幂等性测试。

背景（2026-11-12 真机事故）：
    钉钉把**同一份表单**投递了两次 —— 我们回写卡片状态（finish_ask_card 的 PUT）
    就会触发一次带旧表单值的回调。第二次照单执行 → 提案已归档 → 报「提案不存在」，
    于是把一次**已经成功**的审批显示成 ❌（10-26、11-12 各中一次，真机日志为准）。

本文件钉住三件事：
    1. 提案状态能被认出来（pending / approved / rejected / missing）；
    2. 已结算的提案再批一次 → 判成「重复提交，已忽略」，不是「失败」；
    3. 同一份回调第二次到达 → 整体忽略，**不再执行、不再覆盖卡片终态**。

跑法（仓库根目录）：
    python -m unittest discover -s tests -v
    python tests/test_proposal_idempotency.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools import apply_proposal as ap  # noqa: E402
from tools import memory_store as ms  # noqa: E402
from unittest import mock  # noqa: E402

LOG = logging.getLogger("test_proposal_idempotency")


class _TempMemory:
    """把记忆库整体挪到临时目录 —— 测试绝不能写生产记忆库。

    2026-11-09 踩过坑：只改一份模块实例的路径，测试数据直接写进了 var/qvix 真实库。
    这里改的是**模块属性**，而所有路径函数都在运行时读这些属性，不存在第二份实例。
    """

    def __enter__(self):
        self.tmp = tempfile.mkdtemp(prefix="opencode_mem_")
        self._old = {
            "MEMORY_DIR": ms.MEMORY_DIR,
            "PROPOSAL_DIR": ms.PROPOSAL_DIR,
            "REJECTED_DIR": ms.REJECTED_DIR,
            "JOURNAL_PATH": ms.JOURNAL_PATH,
            "PROFILE_PATH": ms.PROFILE_PATH,
        }
        mem = self.tmp
        ms.MEMORY_DIR = mem
        ms.PROPOSAL_DIR = os.path.join(mem, "proposals")
        ms.REJECTED_DIR = os.path.join(mem, "rejected")
        ms.JOURNAL_PATH = os.path.join(mem, "journal.md")
        ms.PROFILE_PATH = os.path.join(mem, "profile.md")
        os.makedirs(ms.PROPOSAL_DIR, exist_ok=True)
        os.makedirs(ms.REJECTED_DIR, exist_ok=True)
        return self

    def __exit__(self, *exc):
        for k, v in self._old.items():
            setattr(ms, k, v)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False


def _write_proposal(pid: str, status: str = "pending", title: str = "示例提案") -> str:
    """往（临时）记忆库里塞一条提案；status=approved 落到 archive/。"""
    p = {
        "id": pid,
        "type": "memory",
        "title": title,
        "scope": "project:dingtalk-agent",
        "category": "gotcha",
        "confidence": "high",
        "body": "触发条件: 测试。\n动作: 测试。",
        "evidence": "这是一句足够长的测试原话，用于去重闸门。",
        "status": status,
    }
    if status == "approved":
        dst = os.path.join(ms.PROPOSAL_DIR, "archive")
        os.makedirs(dst, exist_ok=True)
        path = os.path.join(dst, f"{pid}.json")
    elif status == "rejected":
        path = os.path.join(ms.REJECTED_DIR, f"{pid}.json")
    else:
        path = ms.proposal_path(pid)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(p, fh, ensure_ascii=False, indent=2)
    return path


class TestProposalState(unittest.TestCase):
    """提案状态判定：pending / approved / rejected / missing。"""

    def test_states(self):
        with _TempMemory():
            _write_proposal("prop_pending_1", "pending")
            _write_proposal("prop_ok_1", "approved")
            _write_proposal("prop_no_1", "rejected")
            self.assertEqual(ms.proposal_state("prop_pending_1"), "pending")
            self.assertEqual(ms.proposal_state("prop_ok_1"), "approved")
            self.assertEqual(ms.proposal_state("prop_no_1"), "rejected")
            self.assertEqual(ms.proposal_state("prop_never_1"), "missing")

    def test_decided_lookup_finds_archive_and_rejected(self):
        with _TempMemory():
            _write_proposal("prop_ok_2", "approved")
            _write_proposal("prop_no_2", "rejected")
            self.assertEqual(ms.load_decided_proposal("prop_ok_2")["status"], "approved")
            self.assertEqual(ms.load_decided_proposal("prop_no_2")["status"], "rejected")
            self.assertIsNone(ms.load_decided_proposal("prop_nothing_2"))


class TestIdempotentActions(unittest.TestCase):
    """已结算的提案再批一次 → 「重复提交，已忽略」，不是「提案不存在」。"""

    def test_approve_on_approved_proposal_is_noop(self):
        with _TempMemory():
            _write_proposal("prop_ok_3", "approved", title="已落地的提案")
            with mock.patch.object(ap, "git") as git:
                ok, msg = ap.approve("prop_ok_3")
            self.assertTrue(ok, msg)
            self.assertIn("重复提交", msg)
            self.assertNotIn("不存在", msg)
            git.assert_not_called()          # 不能再产生一个 commit

    def test_approve_on_rejected_proposal_is_noop(self):
        with _TempMemory():
            _write_proposal("prop_no_3", "rejected")
            ok, msg = ap.approve("prop_no_3")
            self.assertTrue(ok, msg)
            self.assertIn("重复提交", msg)

    def test_reject_on_approved_proposal_is_noop(self):
        with _TempMemory():
            _write_proposal("prop_ok_4", "approved")
            ok, msg = ap.reject("prop_no_such", note="x")   # 真的不存在 → 仍应报错
            self.assertFalse(ok)
            self.assertIn("不存在", msg)
            ok, msg = ap.reject("prop_ok_4", note="x")
            self.assertTrue(ok, msg)
            self.assertIn("重复提交", msg)

    def test_defer_on_approved_proposal_does_not_resurrect(self):
        """暂缓一条已批准的提案 → 绝不能把它写回 pending 目录（变回待审批）。"""
        with _TempMemory():
            _write_proposal("prop_ok_5", "approved")
            ok, msg = ap.defer("prop_ok_5")
            self.assertTrue(ok, msg)
            self.assertIn("重复提交", msg)
            self.assertFalse(os.path.exists(ms.proposal_path("prop_ok_5")),
                             "已批准的提案不能被 defer 写回待审批目录")
            self.assertEqual(ms.proposal_state("prop_ok_5"), "approved")

    def test_unknown_pid_still_reports_missing(self):
        """真的不存在 id → 仍然报「提案不存在」（别把真错误也吞掉）。"""
        with _TempMemory():
            ok, msg = ap.approve("prop_ghost_1")
            self.assertFalse(ok)
            self.assertIn("提案不存在", msg)


class TestBridgeDuplicateCallback(unittest.TestCase):
    """桥接层：同一份表单第二次到达 → 整体忽略，卡片终态不被覆盖。"""

    def setUp(self):
        from tools import opencode_bridge as bridge
        self.bridge = bridge
        bridge._EVOLVE_SEEN.clear()

    def tearDown(self):
        self.bridge._EVOLVE_SEEN.clear()

    @staticmethod
    def _params(value="approve"):
        return {"form": {"p0": {"index": 0, "value": value}}}

    def test_second_identical_callback_is_ignored(self):
        params = self._params()
        action = "evolve:batch:prop_dup_1"
        with mock.patch("tools.apply_proposal.approve",
                        return_value=(True, "已落地 mem_x")) as approve, \
             mock.patch("tools.card_v2.finish_ask_card", return_value=True) as finish:
            first = self.bridge._handle_evolve_proposal("", action, params,
                                                       log=LOG, out_track="oc_t1")
            second = self.bridge._handle_evolve_proposal("", action, params,
                                                        log=LOG, out_track="oc_t1")
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(approve.call_count, 1, "同一份表单只能执行一次")
        self.assertEqual(finish.call_count, 1, "卡片终态不能被第二次回调覆盖")

    def test_different_answer_is_processed(self):
        """换了答案（用户改了选择）→ 必须照常执行，不能被去重挡掉。"""
        with mock.patch("tools.apply_proposal.approve",
                        return_value=(True, "已落地 mem_y")) as approve, \
             mock.patch("tools.card_v2.finish_ask_card", return_value=True):
            self.bridge._handle_evolve_proposal("", "evolve:batch:prop_dup_2",
                                               self._params("approve"),
                                               log=LOG, out_track="oc_t2")
            self.bridge._handle_evolve_proposal("", "evolve:batch:prop_dup_2",
                                               self._params("reject"),
                                               log=LOG, out_track="oc_t2")
        self.assertEqual(approve.call_count, 1)   # 第二次是 reject，不走 approve

    def test_duplicate_on_settled_proposal_says_repeat_not_missing(self):
        """提案已批准 → 卡片上写「重复提交，已忽略」，不能再出现「❌ 提案不存在」。"""
        params = self._params()
        action = "evolve:batch:prop_dup_3"
        with mock.patch("tools.memory_store.proposal_state", return_value="approved"), \
             mock.patch("tools.apply_proposal.approve") as approve, \
             mock.patch("tools.card_v2.finish_ask_card", return_value=True) as finish:
            self.bridge._handle_evolve_proposal("", action, params,
                                               log=LOG, out_track="oc_t3")
        approve.assert_not_called()
        summary = finish.call_args.args[2] if finish.call_args else ""
        self.assertIn("重复提交", summary)
        self.assertNotIn("提案不存在", summary)


if __name__ == "__main__":
    unittest.main(verbosity=2)
