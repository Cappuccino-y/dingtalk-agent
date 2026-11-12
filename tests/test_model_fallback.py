"""
兜底模型选择逻辑的单元测试。

背景（2026-11-12 真机事故）：
    火山 Agent Plan 订阅到期 → 主模型秒失败。桥接层本来有「自动切兜底模型
    再重试」的机制，但当时的守卫写的是
        `cur_pid != FALLBACK_MODEL["providerID"]`
    —— 只比 providerID。Agent Plan 到期后默认值和兜底值都改成了
    minimax-cn-coding-plan（同一个 provider），于是这条守卫**恒为 False**，
    兜底分支根本进不去 → 用户只收到「（空回复）」。

修法：
    ① 兜底改成**候选链**，且**跨 provider 优先**（同一个 provider 一般共用
       同一个订阅/配额池，主模型报额度耗尽时同 provider 的替补大概率一起挂）；
    ② 守卫改成精确比对 (providerID, id)，且只在「已经就是兜底模型本身」时跳过；
    ③ 兜底 id 必须在本机 /api/model 清单里真实存在（切换接口不校验 id）。

跑法（仓库根目录）：
    python -m unittest discover -s tests -v
    python tests/test_model_fallback.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools import model_fallback as mf  # noqa: E402


def _avail(*pairs):
    return [{"providerID": p, "id": i} for p, i in pairs]


class TestCandidates(unittest.TestCase):
    def test_returns_chain_head_when_available(self):
        avail = _avail(("minimax-cn-coding-plan", "MiniMax-M3.1-Flash"),
                       ("minimax2", "MiniMax-M3"))
        got = mf.candidates(avail, ("minimax-cn-coding-plan", "MiniMax-M3.1-Flash"))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["providerID"], "minimax2")

    def test_skips_models_absent_from_local_list(self):
        """切换接口不校验 id：写了不存在的 id 也会返回 204「切成功」，
        但下一轮压根不产出 assistant 消息 → 用户只拿到一张空卡片。"""
        avail = _avail(("minimax-cn-coding-plan", "MiniMax-M3.1-Flash"))
        got = mf.candidates(avail, ("minimax-cn-coding-plan", "MiniMax-M3.1-Flash"))
        self.assertEqual(got, [])

    def test_same_provider_candidate_is_demoted(self):
        """同 provider = 同一个订阅/配额池 → 主模型额度耗尽时它多半也挂，
        必须排在跨 provider 的候选后面。"""
        avail = _avail(("minimax-cn-coding-plan", "MiniMax-M3"),
                       ("deepseek", "deepseek-v4-pro"))
        got = mf.candidates(avail, ("minimax-cn-coding-plan", "MiniMax-M3.1-Flash"))
        self.assertEqual(got[0]["providerID"], "deepseek")
        self.assertEqual(got[1]["providerID"], "minimax-cn-coding-plan")

    def test_skips_the_failed_model_itself(self):
        avail = _avail(("minimax2", "MiniMax-M3"))
        got = mf.candidates(avail, ("minimax2", "MiniMax-M3"))
        self.assertEqual(got, [])

    def test_empty_model_list_refuses_to_guess(self):
        """取不到清单 = 无法验证 id 存在 → 宁可不切，也不能切个不存在的。"""
        self.assertEqual(mf.candidates([], ("minimax-cn-coding-plan", "x")), [])

    def test_chain_contains_no_duplicate_pairs(self):
        seen = set()
        for c in mf.FALLBACK_CHAIN:
            pair = (c["providerID"], c["id"])
            self.assertNotIn(pair, seen)
            seen.add(pair)


class TestFallbackModelConstant(unittest.TestCase):
    def test_fallback_model_is_chain_head(self):
        self.assertEqual(mf.FALLBACK_MODEL["providerID"],
                         mf.FALLBACK_CHAIN[0]["providerID"])
        self.assertEqual(mf.FALLBACK_MODEL["id"], mf.FALLBACK_CHAIN[0]["id"])

    def test_fallback_model_differs_from_default_provider(self):
        """默认模型和兜底不能是同一个 provider —— 那正是 11-12 事故的形状。"""
        self.assertNotEqual(mf.FALLBACK_MODEL["providerID"],
                            "minimax-cn-coding-plan")


class _FakeOC:
    """假的 OpenCodeService：只提供 models()，够 resolve_fallback_candidates 用。"""

    def __init__(self, models):
        self._models = models

    def models(self):
        return self._models


class TestResolveFallbackCandidates(unittest.TestCase):
    """桥接层的候选解析：清单取不到 / id 不存在时必须拒绝瞎切。"""

    @classmethod
    def setUpClass(cls):
        from tools import opencode_bridge as bridge
        cls.bridge = bridge

    def test_real_local_model_list_yields_cross_provider_candidate(self):
        """11-12 事故的形状：主模型 minimax-cn-coding-plan 额度耗尽，
        必须能挑出一个**别的 provider** 的兜底（而不是被守卫挡掉）。"""
        avail = _avail(("minimax-cn-coding-plan", "MiniMax-M3.1-Flash"),
                       ("minimax2", "MiniMax-M3"),
                       ("deepseek", "deepseek-v4-pro"))
        cands, err = self.bridge.resolve_fallback_candidates(
            _FakeOC(avail), ("minimax-cn-coding-plan", "MiniMax-M3.1-Flash"))
        self.assertEqual(err, "")
        self.assertTrue(cands)
        self.assertNotEqual(cands[0]["providerID"], "minimax-cn-coding-plan")

    def test_refuses_when_model_list_unavailable(self):
        cands, err = self.bridge.resolve_fallback_candidates(
            _FakeOC([]), ("minimax-cn-coding-plan", "MiniMax-M3.1-Flash"))
        self.assertEqual(cands, [])
        self.assertTrue(err)

    def test_refuses_when_chain_ids_absent_locally(self):
        avail = _avail(("minimax-cn-coding-plan", "MiniMax-M3.1-Flash"))
        cands, err = self.bridge.resolve_fallback_candidates(
            _FakeOC(avail), ("minimax-cn-coding-plan", "MiniMax-M3.1-Flash"))
        self.assertEqual(cands, [])
        self.assertTrue(err)

    def test_tried_set_is_honored(self):
        """一轮里连换多个候选时，已经试过的不能再试一遍。"""
        avail = _avail(("minimax2", "MiniMax-M3"),
                       ("deepseek", "deepseek-v4-pro"))
        first = ("minimax2", "MiniMax-M3")
        cands, err = self.bridge.resolve_fallback_candidates(
            _FakeOC(avail), ("minimax-cn-coding-plan", "MiniMax-M3.1-Flash"),
            {first})
        self.assertEqual(err, "")
        self.assertNotIn(first, {(c["providerID"], c["id"]) for c in cands})


if __name__ == "__main__":
    unittest.main(verbosity=2)
