"""
evolve 管辖范围闸门 + 去重对照源的单元测试。

跑法（仓库根目录）：
    python -m unittest discover -s tests -v
    python tests/test_evolve_scope.py

背景：2026-10-24 用户反馈「evolve 提的全是之前已经确定修改过的」，
并且「evolve 应该专注于 bot 自身的架构或机制优化」——
所以提案器必须只放行 bot 自身机制类提案，具体业务工作一律丢掉。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools import evolve  # noqa: E402
from tools import memory_store as ms  # noqa: E402
from unittest import mock  # noqa: E402


# 2026-10-24 11:56 那轮真实产出的 3 条提案（原文照抄）
REAL_PROPOSALS = [
    {
        "title": "IC Monitor 的融资 -3% 只能用于强弱分级，不能作为硬剔除条件",
        "body": "触发条件: IC Monitor 遇到融资指标变化达到 -3%。\n动作: 将 ≤-3% 标记为融资强弱信号。"
                "适用范围: DingTalk Agent 的 IC Monitor 信号生成与历史回测。",
        "evidence": "用融资条件-3去剔除的话，是不是会剔除一些有效样本？",
    },
    {
        "title": "IC Monitor 遇到 QVIX 缺日或长假前夜数据时应延后判定",
        "body": "触发条件: IC Monitor 的 QVIX 计算窗口缺少交易日。\n动作: QVIX 窗口有缺日时不进行信号判定。"
                "适用范围: DingTalk Agent 的 IC Monitor 信号生成与历史回测。",
        "evidence": "OK，全部改动一下",
    },
    {
        "title": "GP/JP 生效不代表技能 SP 消耗已修改",
        "body": "触发条件: 修改 GP、JP 或技能消耗相关的数值。\n动作: 将技能 SP 消耗作为独立的数据链路定位。"
                "适用范围: 涉及资源获取、战斗收益和技能蓝耗调整的数值修改与验收。",
        "evidence": "不对呀，消耗的SP还是原来的数字。",
    },
]

# bot 自身机制类 —— 必须放行
BOT_PROPOSALS = [
    {
        "title": "Evolve 作业应避开凌晨并在开机后补跑",
        "body": "触发条件: 凌晨机器没开机。\n动作: 改成白天跑并加长补跑窗口。"
                "适用范围: Bot 作业调度与看门狗。",
        "evidence": "凌晨 3 点永远跑不到",
    },
    {
        "title": "作业超时后必须标记为截断，不能将部分正文视为完成",
        "body": "触发条件: Bot 作业或工具调用达到等待上限。\n动作: 使用明确的 timed_out 状态。"
                "适用范围: DingTalk Agent 的长耗时作业等待、回复状态和卡片展示。",
        "evidence": "等超时时不要把半截正文当最终回答",
    },
    {
        "title": "IC Monitor 推送成功后不得因编码异常误报失败",
        "body": "触发条件: ic_monitor 已成功推送信号，但进程继续输出非 ASCII 文本。"
                "动作: 面向下游的信号输出固定为纯 ASCII。适用范围: 推送与退出码判定。",
        "evidence": "退出码 1 但推送其实成功了",
    },
]


class TestScopeGate(unittest.TestCase):
    """范围闸门：业务域 / 别的项目 → 丢；bot 自身机制 → 留。"""

    def test_business_proposals_are_dropped(self):
        kept = evolve.filter_scope(REAL_PROPOSALS, journal=False)
        self.assertEqual(kept, [])
        for p in REAL_PROPOSALS:
            ok, why = evolve.in_scope(p)
            self.assertFalse(ok, f"应该被丢掉: {p['title']}")
            self.assertTrue(why)

    def test_bot_mechanism_proposals_are_kept(self):
        kept = evolve.filter_scope(BOT_PROPOSALS, journal=False)
        self.assertEqual(len(kept), len(BOT_PROPOSALS))
        for p in BOT_PROPOSALS:
            ok, _ = evolve.in_scope(p)
            self.assertTrue(ok, f"应该被放行: {p['title']}")

    def test_empty_proposal_list(self):
        self.assertEqual(evolve.filter_scope([], journal=False), [])


class TestHandledSummaries(unittest.TestCase):
    """去重对照源：驳回 / 暂缓 / 已落地 三种状态都要出现（别再重提）。"""

    def test_contains_all_three_states(self):
        out = ms.handled_summaries(50)
        blob = "\n".join(out)
        self.assertIn("[rejected]", blob)
        self.assertIn("[approved]", blob)
        # 暂缓的不一定有（取决于当时有没有暂缓过），但格式必须对
        for line in out:
            self.assertTrue(line.startswith("- ["), line)


class TestDoneItems(unittest.TestCase):
    """「近期已完成改动」对照表：journal 里的完成标记要能被认出来。"""

    def test_marks_completed_entries(self):
        journal = [{
            "ts": "2026-10-23T10:40:11",
            "summary": "提案已落地: IC Monitor 推送成功后不得因编码异常误报失败",
            "fields": {"answer": "已写入记忆库"},
        }, {
            "ts": "2026-10-24T11:00:00",
            "summary": "今天天气不错",
            "fields": {"answer": "-"},
        }]
        items = evolve.done_items(journal)
        self.assertEqual(len(items), 1)
        self.assertIn("IC Monitor", items[0])


class TestNoProposalNotice(unittest.TestCase):
    """没有提案时也要推一条通知（用户 2026-11-02：「无论有没有值得生成的改动，
    是不是至少给推个卡片通知」—— 否则连着几天安静，分不清是没问题还是挂了）。"""

    EV = {
        "journal": [{}, {}, {}],
        "recent_qa": [{}, {}],
        "handled_proposals": ["- [rejected] 示例"],
    }

    def test_notice_reports_evidence_counts(self):
        with mock.patch("tools.dingtalk_push.push_markdown",
                        return_value=True) as push:
            ok = evolve.notify_no_proposal(self.EV, model="MiniMax-M3.1-Flash",
                                           repushed=1)
        self.assertTrue(ok)
        text = push.call_args.args[0]
        self.assertIn("交互流水 3 条", text)
        self.assertIn("问答存档 2 条", text)
        self.assertIn("重推了 1 张", text)
        self.assertIn("没有新提案", text)

    def test_dry_run_never_pushes(self):
        with mock.patch("tools.dingtalk_push.push_markdown") as push:
            ok = evolve.notify_no_proposal(self.EV, dry_run=True)
        self.assertFalse(ok)
        push.assert_not_called()


class TestJournalTurnEvidence(unittest.TestCase):
    """对话轮次写进 journal 后，必须真的出现在提案器的证据里（A 方案闭环）。"""

    def test_qa_turn_reaches_prompt(self):
        ev = {
            "journal": [{
                "ts": "2026-11-02T14:00:00", "kind": "qa_turn",
                "summary": "为什么 evolve 没跑",
                "fields": {"answer": "它跑了，0 条提案；已补对话证据源"},
            }],
            "rejected_proposals": [],
            "handled_proposals": [],
            "done_items": [],
            "memory_index_global": "（空）",
            "memory_index_project": "（空）",
            "recent_qa": [],
        }
        prompt = evolve.build_prompt(ev)
        self.assertIn("为什么 evolve 没跑", prompt)
        self.assertIn("已补对话证据源", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
