"""
「失败回合也必须进 journal（evolve 的证据源）」的回归测试。

背景（2026-11-12 真机事故）：
    早上 09:00 主模型订阅过期 → 桥接层这一轮 finish=error，用户只收到 5 个字
    「（空回复）」。而当时 journal 的写入规则有一句
        「（空回复）」这种没有信息量的不记，免得污染证据
    → **这次失败被证据源显式排除**。结果 journal.md 里 11-12 上午
    （00:00–09:59）一条记录都没有，evolve 10:01 扫描时压根看不见这次异常，
    自然提不出对应提案（只能提一条误引了旧对话的「空回复」提案）。

修法：
    失败回合（finish=error / 只落了占位符 / 被中断）**照记**，
    并带上**真实原因**（模型 + 报错摘要），kind 记成 qa_error；
    正常回合原样记（kind=qa_turn）。

跑法（仓库根目录）：
    python -m unittest discover -s tests -v
    python tests/test_turn_journal.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools import opencode_bridge as bridge  # noqa: E402


class TestTurnJournalRecord(unittest.TestCase):

    def test_normal_answer_kept_as_qa_turn(self):
        ans, kind = bridge.turn_journal_record("这是正常回答。")
        self.assertEqual(kind, "qa_turn")
        self.assertEqual(ans, "这是正常回答。")

    def test_error_turn_is_recorded_with_reason(self):
        """09:00 那轮的形状：finish=error，真实原因在 message 里。"""
        ans, kind = bridge.turn_journal_record(
            "（空回复）", finish="error", model="deepseek-v4.1-flash",
            detail="does not have a valid AgentPlan subscription")
        self.assertEqual(kind, "qa_error")
        self.assertIn("deepseek-v4.1-flash", ans)
        self.assertIn("AgentPlan", ans)
        self.assertTrue(ans.strip(), "失败回合绝不能记成空")

    def test_placeholder_reply_is_not_silently_dropped(self):
        """老规则把「（空回复）」直接跳过 —— 现在必须留下痕迹。"""
        ans, kind = bridge.turn_journal_record("（空回复）")
        self.assertEqual(kind, "qa_error")
        self.assertTrue(ans.strip())

    def test_error_without_detail_still_recorded(self):
        ans, kind = bridge.turn_journal_record(
            "（空回复）", finish="error", model="x", detail="")
        self.assertEqual(kind, "qa_error")
        self.assertIn("无错误详情", ans)

    def test_interrupted_turn_recorded(self):
        ans, kind = bridge.turn_journal_record(
            "🛑 已按你的要求中断。", interrupted=True)
        self.assertEqual(kind, "qa_error")
        self.assertIn("中断", ans)

    def test_empty_answer_recorded(self):
        ans, kind = bridge.turn_journal_record("")
        self.assertEqual(kind, "qa_error")
        self.assertTrue(ans.strip())

    def test_new_error_text_is_recorded_as_qa_error(self):
        """9d06c67 之后，finish=error 的可见文本已不是「（空回复）」，
        而是带真实原因的「⚠️ 模型报错…」。这一轮同样必须进证据源，
        并且仍然标成 qa_error（它确实是失败回合）。"""
        text = ("⚠️ 模型报错，这一轮没有回复。\n\n"
                "`does not have a valid AgentPlan subscription`")
        ans, kind = bridge.turn_journal_record(
            text, finish="error", model="deepseek-v4.1-flash",
            detail="does not have a valid AgentPlan subscription")
        self.assertEqual(kind, "qa_error")
        self.assertIn("AgentPlan", ans)
        self.assertIn("deepseek-v4.1-flash", ans)

    def test_journal_kind_reaches_the_file(self):
        """kind=qa_error 必须真的写进 journal（evolve 靠它区分失败回合）。

        写到临时文件，不碰真 journal.md（那是 evolve 的证据源，不能被测试污染）。"""
        import tempfile
        from tools import memory_store as ms
        old = ms.JOURNAL_PATH
        with tempfile.TemporaryDirectory() as td:
            ms.JOURNAL_PATH = str(Path(td) / "journal.md")
            try:
                bridge.journal_turn("conv-test", "你好",
                                     "这一轮失败：模型 x；原因：订阅过期",
                                     session_id="ses-test", kind="qa_error")
                recs = ms.read_journal(2000)
            finally:
                ms.JOURNAL_PATH = old
        self.assertEqual(len(recs), 1)
        last = recs[0]
        self.assertEqual(last["kind"], "qa_error")
        self.assertIn("订阅过期", last["fields"].get("answer", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
