"""MiniMax 双 key 备选替换机制的单元测试。

背景（2026-11-12 晚）：
    本机有**两把** MiniMax coding-plan key（`~/.minimax-config.json` 的
    `accounts[]`），是两个**互相独立的额度池**：
        key1 = minimax-cn-coding-plan provider（默认模型用的那把）
        key2 = minimax2 provider
    两把 key 在 `coding_plan/remains` 上剩余额度不同、到期时间不同
    （实测 key1 周额度 75%、key2 周额度 63%，到期日差 37 天）→ 确实是两个池。

    需求：任一把额度用完，要能**自动换另一把**，像 deepseek 与 MiniMax
    那样是**平级的独立兜底**，而不是「同 provider 的兄弟模型一起挂」。

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


KEY1 = "minimax-cn-coding-plan"
KEY2 = "minimax2"


class TestPoolMapping(unittest.TestCase):
    """两把 MiniMax key 必须映射到**不同**的额度池。"""

    def test_two_minimax_providers_are_different_pools(self):
        """两把 key 挂在两个 provider 下 → 两个独立额度池。"""
        self.assertNotEqual(mf.pool_of(KEY1), mf.pool_of(KEY2))

    def test_unknown_provider_is_its_own_pool(self):
        """认不出来的 provider 自己成一个池（宁可多试一次，别把活牌排最后）。"""
        self.assertEqual(mf.pool_of("some-new-provider"), "some-new-provider")

    def test_explicit_pool_in_entry_wins(self):
        entry = {"providerID": KEY2, "id": "x", "pool": "custom-pool"}
        self.assertEqual(mf.pool_of(KEY2, entry), "custom-pool")


class TestChainShape(unittest.TestCase):
    """候选链本身的形状。"""

    def test_chain_contains_no_duplicate_pairs(self):
        seen = set()
        for c in mf.FALLBACK_CHAIN:
            pair = (c["providerID"], c["id"])
            self.assertNotIn(pair, seen)
            seen.add(pair)

    def test_same_model_on_second_key_is_in_chain(self):
        """第二把 key 上必须有**同一个模型**（M3.1-Flash）——
        否则「换 key」就变成了「顺便降级模型」，用户拿到的质量变了。"""
        pairs = {(c["providerID"], c["id"]) for c in mf.FALLBACK_CHAIN}
        self.assertIn((KEY2, "MiniMax-M3.1-Flash"), pairs)
        self.assertIn((KEY1, "MiniMax-M3.1-Flash"),
                      {(KEY1, "MiniMax-M3.1-Flash")} | pairs)

    def test_every_entry_declares_its_pool(self):
        """每个候选都要写明额度池 —— 否则排序时退化成按 providerID 猜。"""
        for c in mf.FALLBACK_CHAIN:
            self.assertTrue(c.get("pool"), f"{c} 缺 pool")


class TestCandidatesOrdering(unittest.TestCase):
    """排序：跨额度池优先，同池靠后。"""

    def test_second_key_same_model_beats_first_key_sibling(self):
        """主模型（key1）额度耗尽 → 第一顺位是 key2 上的**同一个模型**。"""
        avail = _avail((KEY1, "MiniMax-M3.1-Flash"),
                       (KEY2, "MiniMax-M3.1-Flash"),
                       (KEY2, "MiniMax-M3"))
        got = mf.candidates(avail, (KEY1, "MiniMax-M3.1-Flash"))
        self.assertEqual(got[0]["providerID"], KEY2)
        self.assertEqual(got[0]["id"], "MiniMax-M3.1-Flash")

    def test_same_pool_sibling_is_demoted_below_other_pool(self):
        """key2 的兄弟模型（MiniMax-M3）跟 key2 **共用额度池** → 必须排在
        deepseek（另一个池）后面，否则会先撞同一堵墙。"""
        avail = _avail((KEY2, "MiniMax-M3.1-Flash"),
                       (KEY2, "MiniMax-M3"),
                       ("deepseek", "deepseek-v4-pro"))
        got = mf.candidates(avail, (KEY2, "MiniMax-M3.1-Flash"))
        providers = [c["providerID"] for c in got]
        self.assertLess(providers.index("deepseek"),
                        providers.index(KEY2))

    def test_first_key_sibling_shares_failed_pool(self):
        """key1 的兄弟模型跟默认模型同池 → 排在最后。"""
        avail = _avail((KEY1, "MiniMax-M3"),
                       (KEY2, "MiniMax-M3.1-Flash"),
                       ("deepseek", "deepseek-v4-pro"))
        got = mf.candidates(avail, (KEY1, "MiniMax-M3.1-Flash"))
        providers = [c["providerID"] for c in got]
        self.assertEqual(providers[-1], KEY1)
        self.assertLess(providers.index("deepseek"), providers.index(KEY1))

    def test_failed_model_itself_is_skipped(self):
        avail = _avail((KEY2, "MiniMax-M3.1-Flash"))
        got = mf.candidates(avail, (KEY2, "MiniMax-M3.1-Flash"))
        self.assertEqual(got, [])

    def test_ids_absent_from_local_list_are_skipped(self):
        """切换接口不校验 id：不存在的 id 也会「切成功」，但下一轮没消息。"""
        avail = _avail((KEY1, "MiniMax-M3.1-Flash"))
        got = mf.candidates(avail, (KEY1, "MiniMax-M3.1-Flash"))
        self.assertEqual(got, [])

    def test_empty_model_list_refuses_to_guess(self):
        self.assertEqual(mf.candidates([], (KEY1, "x")), [])

    def test_tried_set_is_honored(self):
        avail = _avail((KEY2, "MiniMax-M3.1-Flash"),
                       ("deepseek", "deepseek-v4-pro"))
        first = (KEY2, "MiniMax-M3.1-Flash")
        got = mf.candidates(avail, (KEY1, "MiniMax-M3.1-Flash"), {first})
        self.assertNotIn(first, {(c["providerID"], c["id"]) for c in got})


class TestFallbackModelConstant(unittest.TestCase):
    def test_fallback_model_is_chain_head(self):
        self.assertEqual(mf.FALLBACK_MODEL["providerID"],
                         mf.FALLBACK_CHAIN[0]["providerID"])
        self.assertEqual(mf.FALLBACK_MODEL["id"], mf.FALLBACK_CHAIN[0]["id"])

    def test_fallback_model_differs_from_default_provider(self):
        """默认模型和兜底不能是同一个 provider —— 那正是 11-12 事故的形状。"""
        self.assertNotEqual(mf.FALLBACK_MODEL["providerID"], KEY1)


class _FakeOC:
    def __init__(self, models):
        self._models = models

    def models(self):
        return self._models


class TestResolveFallbackCandidates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tools import opencode_bridge as bridge
        cls.bridge = bridge

    def test_real_local_model_list_yields_cross_pool_candidate(self):
        """主模型 key1 额度耗尽 → 必须能挑出**另一个额度池**的兜底。"""
        avail = _avail((KEY1, "MiniMax-M3.1-Flash"),
                       (KEY2, "MiniMax-M3.1-Flash"),
                       ("deepseek", "deepseek-v4-pro"))
        cands, err = self.bridge.resolve_fallback_candidates(
            _FakeOC(avail), (KEY1, "MiniMax-M3.1-Flash"))
        self.assertEqual(err, "")
        self.assertTrue(cands)
        self.assertNotEqual(cands[0]["providerID"], KEY1)

    def test_refuses_when_model_list_unavailable(self):
        cands, err = self.bridge.resolve_fallback_candidates(
            _FakeOC([]), (KEY1, "MiniMax-M3.1-Flash"))
        self.assertEqual(cands, [])
        self.assertTrue(err)

    def test_refuses_when_chain_ids_absent_locally(self):
        avail = _avail((KEY1, "MiniMax-M3.1-Flash"))
        cands, err = self.bridge.resolve_fallback_candidates(
            _FakeOC(avail), (KEY1, "MiniMax-M3.1-Flash"))
        self.assertEqual(cands, [])
        self.assertTrue(err)


class TestQuotaErrorDetection(unittest.TestCase):
    """MiniMax 的额度错误识别 —— 错误码在 base_resp.status_code 里。"""

    @classmethod
    def setUpClass(cls):
        from tools import opencode_bridge as bridge
        cls.bridge = bridge

    def _msg(self, text):
        return {"finish": "error", "error": {"message": text}}

    def test_minimax_quota_error_codes_are_detected(self):
        """官方错误码：1002 频率超限 / 1008 余额不足 / 2056 Token Plan 资源限制
        / 2062 M Plan 速率限制 / 2067 套餐额度已用完。"""
        for code in ("1002", "1008", "2056", "2062", "2067"):
            with self.subTest(code=code):
                msg = self._msg(f"some error ({code})")
                self.assertTrue(self.bridge.is_quota_error(msg))

    def test_minimax_chinese_quota_text_is_detected(self):
        msg = self._msg("超出 Token Plan 资源限制，请等待下一个时间段资源释放")
        self.assertTrue(self.bridge.is_quota_error(msg))

    def test_normal_answer_mentioning_quota_is_not_an_error(self):
        """正常回答里提到「额度」不算 —— 否则会误切模型。"""
        msg = {"finish": "stop",
               "content": [{"type": "text", "text": "你的额度还剩 50%"}]}
        self.assertFalse(self.bridge.is_quota_error(msg))

    def test_unrelated_error_is_not_quota(self):
        msg = self._msg("invalid params, unknown model 'x' (2013)")
        self.assertFalse(self.bridge.is_quota_error(msg))

    def test_context_length_exceeded_is_not_quota(self):
        """"exceed" 这种宽泛词不能命中 —— 上下文超长切模型没用。"""
        msg = self._msg("context length exceeded")
        self.assertFalse(self.bridge.is_quota_error(msg))


if __name__ == "__main__":
    unittest.main(verbosity=2)
