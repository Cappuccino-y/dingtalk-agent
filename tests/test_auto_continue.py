"""
「超时不再直接截断，而是自动续做」的回归测试。

背景（2026-11-12 真机事故）：
    12:26 那轮同时干了 373 行桥接层改造 + 148 行新测试 + git commit +
    协同重启 + 翻日志/journal 交叉验证 → 跑了 30 分钟。
    桥接层到 WAIT_TIMEOUT=1800 就**主动截断**，只给用户回了个占位符
    （「回一句继续刚才的」）。而模型**并没有停** —— 12:58 照样提交了
    9d06c67，活干完了，只是用户这边断了线，只能靠用户自己想起来再问一次。

    用户的要求：「对于之前的超时问题需要你能够自动重试，不要回复先停在这里，
    需要你自动接着做」。

改法（口径钉死，测试守着）：
    ① 软上限（默认 30 分钟）= 只**提醒**，**继续等**，绝不截断；
    ② 硬上限（默认 60 分钟）= **自动续做**：给同一个会话补一条
       「接着刚才的活继续」的指令，再开一个新窗口；
    ③ 续做次数用尽 = 才认输收尾（如实告诉用户续做了几次）；
    ④ 被截断的回合必须记进 journal（kind=qa_error）——evolve 要看得见。

跑法（仓库根目录）：
    python -m unittest discover -s tests -v
    python tests/test_auto_continue.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools import evolve  # noqa: E402
from tools import opencode_bridge as bridge  # noqa: E402


class TestWaitPolicy(unittest.TestCase):
    """等待策略：软上限不截断 / 硬上限自动续做 / 次数用尽才认输。"""

    def test_soft_limit_keeps_waiting(self):
        """软上限到点只是提醒 —— 绝不能在这里把这一轮截断。"""
        action, msg = bridge.wait_policy(
            window=bridge.WAIT_TIMEOUT, n_continue=0, waited=bridge.WAIT_TIMEOUT,
            hard=bridge.WAIT_HARD_TIMEOUT, max_continue=bridge.AUTO_CONTINUE_MAX)
        self.assertEqual(action, bridge.WAIT_KEEP)
        self.assertIn("继续等", msg)

    def test_hard_limit_triggers_auto_continue(self):
        action, msg = bridge.wait_policy(
            window=bridge.WAIT_HARD_TIMEOUT, n_continue=0,
            waited=bridge.WAIT_HARD_TIMEOUT,
            hard=bridge.WAIT_HARD_TIMEOUT, max_continue=bridge.AUTO_CONTINUE_MAX)
        self.assertEqual(action, bridge.WAIT_CONTINUE)
        self.assertIn("自动续做", msg)

    def test_gives_up_only_after_continue_budget_exhausted(self):
        action, _ = bridge.wait_policy(
            window=bridge.WAIT_HARD_TIMEOUT,
            n_continue=bridge.AUTO_CONTINUE_MAX,
            waited=bridge.WAIT_HARD_TIMEOUT * (bridge.AUTO_CONTINUE_MAX + 1),
            hard=bridge.WAIT_HARD_TIMEOUT, max_continue=bridge.AUTO_CONTINUE_MAX)
        self.assertEqual(action, bridge.WAIT_GIVEUP)

    def test_below_hard_limit_never_gives_up(self):
        """窗口内任何时刻都不该「认输」——哪怕已经续做过很多次。"""
        for window in (1, 600, bridge.WAIT_TIMEOUT, bridge.WAIT_HARD_TIMEOUT - 1):
            action, _ = bridge.wait_policy(
                window=window, n_continue=0, waited=window,
                hard=bridge.WAIT_HARD_TIMEOUT, max_continue=bridge.AUTO_CONTINUE_MAX)
            self.assertNotEqual(action, bridge.WAIT_GIVEUP, f"window={window}")

    def test_hard_limit_is_not_smaller_than_soft_limit(self):
        """硬上限必须 >= 软上限，否则第一次超时就直接续做（等于没等过）。"""
        self.assertGreaterEqual(bridge.WAIT_HARD_TIMEOUT, bridge.WAIT_TIMEOUT)

    def test_auto_continue_budget_is_bounded(self):
        """续做次数必须有上限，不能无限续做。"""
        self.assertGreaterEqual(bridge.AUTO_CONTINUE_MAX, 1)

    def test_continue_prompt_tells_the_model_to_resume(self):
        p = bridge.AUTO_CONTINUE_PROMPT
        self.assertIn("继续", p)
        self.assertIn("不要重做", p.replace("已经做完的部分不要重做", "不要重做"))


class _FakeOC:
    """假的 OpenCodeService：只实现自动续做用到的三个方法。"""

    def __init__(self, last=None, prompt_ok=True, interrupt_ok=True):
        self._last = last
        self._prompt_ok = prompt_ok
        self._interrupt_ok = interrupt_ok
        self.prompts: list[str] = []
        self.interrupts = 0

    def last_assistant(self, sid):
        return self._last

    def prompt(self, sid, text, files=None):
        if not self._prompt_ok and self.interrupts == 0:
            raise RuntimeError("session is busy")
        self.prompts.append(text)
        return {}

    def interrupt(self, sid, resume=False):
        if not self._interrupt_ok:
            raise RuntimeError("interrupt failed")
        self.interrupts += 1
        return True


class TestAutoContinue(unittest.TestCase):
    """自动续做本身：什么时候该投、投不进去怎么办。"""

    def test_already_answered_is_not_retried(self):
        """这一轮其实已经答完（wait_idle 误报超时）→ 别再投指令。"""
        oc = _FakeOC(last={"id": "new", "finish": "stop"})
        res = bridge.AgentTurn._auto_continue(oc, "sid", "old")
        self.assertEqual(res, "done")
        self.assertEqual(oc.prompts, [])

    def test_continue_prompt_is_delivered(self):
        oc = _FakeOC(last={"id": "old", "finish": "tool-calls"})
        res = bridge.AgentTurn._auto_continue(oc, "sid", "old")
        self.assertEqual(res, "sent")
        self.assertEqual(len(oc.prompts), 1)
        self.assertIn("自动续做", oc.prompts[0])

    def test_busy_session_is_interrupted_then_retried(self):
        """会话忙到投不进去 → 先打断卡住的那轮，再投续做指令。"""
        oc = _FakeOC(last={"id": "old", "finish": "tool-calls"},
                     prompt_ok=False)
        res = bridge.AgentTurn._auto_continue(oc, "sid", "old")
        self.assertEqual(res, "sent")
        self.assertEqual(oc.interrupts, 1)
        self.assertEqual(len(oc.prompts), 1)

    def test_failed_auto_continue_is_reported(self):
        oc = _FakeOC(last={"id": "old", "finish": "tool-calls"},
                     prompt_ok=False, interrupt_ok=False)
        res = bridge.AgentTurn._auto_continue(oc, "sid", "old")
        self.assertEqual(res, "failed")


class TestTruncatedTurnJournal(unittest.TestCase):
    """被截断的回合必须进证据源（kind=qa_error），并带上续做次数。"""

    def test_truncated_turn_is_qa_error(self):
        ans, kind = bridge.turn_journal_record(
            "半截正文", finish="tool-calls", model="glm-5.3-flash",
            truncated=True, auto_continue=3)
        self.assertEqual(kind, "qa_error")
        self.assertIn("超时截断", ans)
        self.assertIn("3", ans)

    def test_normal_turn_stays_qa_turn(self):
        ans, kind = bridge.turn_journal_record("答完了。")
        self.assertEqual(kind, "qa_turn")
        self.assertEqual(ans, "答完了。")


class TestEvolveSeesFailures(unittest.TestCase):
    """evolve：失败回合必须**优先**出现在提案提示词里。"""

    EV = {
        "journal": [{
            "ts": "2026-11-12T09:00:22", "kind": "qa_error",
            "summary": "今天早上那条消息",
            "fields": {"answer": "这一轮失败：finish=error；模型 x；原因：订阅过期"},
        }],
        "failed_turns": [{
            "ts": "2026-11-12T09:00:22", "kind": "qa_error",
            "summary": "今天早上那条消息",
            "fields": {"answer": "这一轮失败：finish=error；模型 x；原因：订阅过期"},
        }],
        "rejected_proposals": [],
        "handled_proposals": [],
        "done_items": [],
        "memory_index_global": "（空）",
        "memory_index_project": "（空）",
        "recent_qa": [],
    }

    def test_failed_turn_has_its_own_priority_section(self):
        prompt = evolve.build_prompt(self.EV)
        self.assertIn("失败回合", prompt)
        self.assertIn("最高优先级", prompt)
        self.assertIn("订阅过期", prompt)

    def test_system_prompt_requires_failed_turn_first(self):
        sp = evolve.SYSTEM_PROMPT
        self.assertIn("失败回合优先提", sp)
        self.assertIn("绝不允许", sp)

    def test_failure_proposal_passes_scope_gate(self):
        """失败回合提出来的提案（模型订阅过期 → 自动兜底）不能被范围闸门丢掉。"""
        p = {
            "title": "主模型订阅过期时必须自动切兜底并留下真实原因",
            "body": "触发条件: 模型返回订阅过期报错。\n动作: 自动切兜底模型并在卡片上"
                    "写出真实原因。\n适用范围: 桥接层的模型兜底与错误呈现。",
            "evidence": "这一轮失败：finish=error；模型 x；原因：订阅过期",
        }
        ok, why = evolve.in_scope(p)
        self.assertTrue(ok, f"不该被范围闸门丢掉：{why}")

    def test_no_proposal_notice_mentions_failures(self):
        from unittest import mock
        with mock.patch("tools.dingtalk_push.push_markdown",
                        return_value=True) as push:
            evolve.notify_no_proposal(self.EV, model="MiniMax-M3.1-Flash")
        text = push.call_args.args[0]
        self.assertIn("失败回合", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
