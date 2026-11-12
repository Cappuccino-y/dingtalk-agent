"""直连 API 路径的 **MiniMax 双 key 轮换** 单元测试。

背景（2026-11-12 晚）：
    OpenCode 侧（聊天回合）走 `tools/model_fallback.py` 的候选链换模型；
    但**作业侧**（早报 / evolve / 公众号日报）是直连 MiniMax 的 OpenAI 兼容
    端点，只用 `MINIMAX_API_KEY` 一把 key —— 额度用完整轮作业直接死。

    这里把 `tools/llm_client.py` 改成带 key 池的客户端：一把 key 报额度类错误
    就自动换下一把继续跑同一个请求。

跑法（仓库根目录）：
    python tests/test_llm_client_rotation.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools import llm_client as lc  # noqa: E402


class FakeRateLimit(Exception):
    """模拟 openai SDK 的 429 异常。"""
    status_code = 429


class FakeBadRequest(Exception):
    """模拟 400 参数错误 —— 跟 key 无关，不该换 key。"""
    status_code = 400


class FakeResp:
    def __init__(self, text="ok", base_resp=None):
        self.text = text
        self.base_resp = base_resp


class FakeCompletions:
    def __init__(self, owner, idx):
        self._owner = owner
        self._idx = idx

    def create(self, **kwargs):
        self._owner.calls.append(self._idx)
        return self._owner.behaviour(self._idx, **kwargs)


class FakeChat:
    def __init__(self, owner, idx):
        self.completions = FakeCompletions(owner, idx)


class FakeClient:
    """假的 openai.OpenAI 客户端（每个对应池子里的一把 key）。"""

    def __init__(self, owner, idx):
        self._owner = owner
        self._idx = idx
        self.chat = FakeChat(owner, idx)


class _RotHarness:
    """造一个只依赖 fake 行为的 RotatingOpenAI（不碰真实网络）。"""

    @staticmethod
    def build(behaviour, n=2):
        rot = lc.RotatingOpenAI.__new__(lc.RotatingOpenAI)
        rot._keys = [f"key{i + 1}" for i in range(n)]
        rot._dead = set()
        rot._idx = 0
        rot.base_url = None
        rot.chat = lc._RotatingChat(rot)
        rot.calls = []
        rot.behaviour = behaviour
        rot._clients = [FakeClient(rot, i) for i in range(n)]
        return rot


class TestQuotaDetection(unittest.TestCase):
    def test_http_429_is_quota(self):
        self.assertTrue(lc._is_quota_error(FakeRateLimit("rate limit")))

    def test_http_402_is_quota(self):
        exc = FakeRateLimit("payment required")
        exc.status_code = 402
        self.assertTrue(lc._is_quota_error(exc))

    def test_minimax_error_codes_are_quota(self):
        for code in ("1002", "1008", "2056", "2062", "2067"):
            with self.subTest(code=code):
                self.assertTrue(lc._is_quota_error(RuntimeError(f"err ({code})")))

    def test_minimax_chinese_quota_text(self):
        self.assertTrue(lc._is_quota_error(
            RuntimeError("超出 Token Plan 资源限制")))

    def test_bad_request_is_not_quota(self):
        """参数错误跟 key 无关 —— 换 key 只是把同一个错误再撞一遍。"""
        self.assertFalse(lc._is_quota_error(FakeBadRequest("unknown model")))

    def test_network_error_is_not_quota(self):
        self.assertFalse(lc._is_quota_error(ConnectionError("timed out")))


class TestIsMinimax(unittest.TestCase):
    def test_minimax_urls_detected(self):
        self.assertTrue(lc._is_minimax("https://api.minimaxi.com/v1"))
        self.assertTrue(lc._is_minimax("https://api.minimaxi.com/anthropic/v1"))

    def test_volcano_url_not_minimax(self):
        """火山的 key 不能混进 MiniMax 的池子（会 401 白转一圈）。"""
        self.assertFalse(lc._is_minimax(
            "https://ark.cn-beijing.volces.com/api/v3"))

    def test_none_is_not_minimax(self):
        self.assertFalse(lc._is_minimax(None))


class TestRotatingOpenAI(unittest.TestCase):
    """轮换行为本身。"""

    def _rot(self, behaviour, n=2):
        return _RotHarness.build(behaviour, n)

    def test_quota_error_switches_to_next_key(self):
        def behaviour(idx, **kw):
            if idx == 0:
                raise FakeRateLimit("exceeded the 5-hour usage quota")
            return FakeResp("from-key-2")

        rot = self._rot(behaviour)
        resp = rot.create(model="x")
        self.assertEqual(resp.text, "from-key-2")
        self.assertEqual(rot.calls, [0, 1])

    def test_dead_key_is_not_retried(self):
        """额度耗尽的 key 判定后，本进程内不再回头用它。"""
        def behaviour(idx, **kw):
            if idx == 0:
                raise FakeRateLimit("quota")
            return FakeResp("ok")

        rot = self._rot(behaviour)
        rot.create(model="x")
        rot.create(model="x")
        # 第二轮不该再撞 key1
        self.assertEqual(rot.calls.count(0), 1)

    def test_non_quota_error_is_not_swallowed(self):
        """参数错误要原样抛出去，不能换 key 硬试。"""
        def behaviour(idx, **kw):
            raise FakeBadRequest("unknown model")

        rot = self._rot(behaviour)
        with self.assertRaises(FakeBadRequest):
            rot.create(model="x")
        self.assertEqual(rot.calls, [0])

    def test_all_keys_exhausted_raises(self):
        def behaviour(idx, **kw):
            raise FakeRateLimit("quota")

        rot = self._rot(behaviour, n=2)
        with self.assertRaises(FakeRateLimit):
            rot.create(model="x")
        self.assertEqual(sorted(rot.calls), [0, 1])


class TestBusinessErrorInBody(unittest.TestCase):
    """MiniMax 把业务错误码放在响应体 base_resp 里，HTTP 仍是 200。

    这种情况下 SDK **不抛异常** —— 不显式拦一下，额度耗尽会静默变成空回复。
    """

    def _rot(self, resp, n=2):
        return _RotHarness.build(lambda idx, **kw: resp, n)

    def test_quota_code_in_body_triggers_switch(self):
        resp = FakeResp("ok", base_resp={"status_code": 2056,
                                         "status_msg": "超出Token Plan资源限制"})
        rot = self._rot(resp)
        with self.assertRaises(RuntimeError) as ctx:
            rot.create(model="x")
        self.assertIn("2056", str(ctx.exception))

    def test_non_quota_business_error_also_raises(self):
        """非额度类的业务错误也要抛 —— 别把空 choices 当正常回复交上去。"""
        resp = FakeResp("ok", base_resp={"status_code": 2013,
                                         "status_msg": "参数错误"})
        rot = self._rot(resp)
        with self.assertRaises(RuntimeError) as ctx:
            rot.create(model="x")
        self.assertIn("2013", str(ctx.exception))

    def test_success_body_passes_through(self):
        resp = FakeResp("hello", base_resp={"status_code": 0,
                                            "status_msg": "success"})
        rot = self._rot(resp)
        out = rot.create(model="x")
        self.assertEqual(out.text, "hello")

    def test_body_without_base_resp_passes_through(self):
        resp = FakeResp("hello")
        rot = self._rot(resp)
        out = rot.create(model="x")
        self.assertEqual(out.text, "hello")


class TestKeyPoolSources(unittest.TestCase):
    """key 池的来源与去重。"""

    def test_pool_reads_minimax_config_accounts(self):
        """权威来源是 ~/.minimax-config.json 的 accounts[]（跟「查余额」同源）。"""
        import json
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".minimax-config.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"accounts": [{"name": "a", "token": "tk-a"},
                                        {"name": "b", "token": "tk-b"}]}, fh)
            old_home = os.environ.get("HOME")
            old_environ = os.environ.get("USERPROFILE")
            try:
                os.environ["HOME"] = tmp
                os.environ["USERPROFILE"] = tmp
                os.environ.pop("MINIMAX_API_KEY", None)
                os.environ.pop("MINIMAX_API_KEYS", None)
                pool = lc._minimax_key_pool()
            finally:
                if old_home is not None:
                    os.environ["HOME"] = old_home
                if old_environ is not None:
                    os.environ["USERPROFILE"] = old_environ
        self.assertIn("tk-a", pool)
        self.assertIn("tk-b", pool)

    def test_pool_dedupes(self):
        import json
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".minimax-config.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"accounts": [{"name": "a", "token": "dup"},
                                        {"name": "b", "token": "dup"}]}, fh)
            old_home = os.environ.get("HOME")
            old_environ = os.environ.get("USERPROFILE")
            try:
                os.environ["HOME"] = tmp
                os.environ["USERPROFILE"] = tmp
                os.environ["MINIMAX_API_KEY"] = "dup"
                os.environ.pop("MINIMAX_API_KEYS", None)
                pool = lc._minimax_key_pool()
            finally:
                if old_home is not None:
                    os.environ["HOME"] = old_home
                if old_environ is not None:
                    os.environ["USERPROFILE"] = old_environ
        self.assertEqual(pool.count("dup"), 1)

    def test_missing_config_file_is_not_fatal(self):
        """配置文件不存在/坏了 → 只用 env 的 key，不能让整个客户端起不来。"""
        import os
        old_home = os.environ.get("HOME")
        old_environ = os.environ.get("USERPROFILE")
        try:
            os.environ["HOME"] = "/nonexistent-home-dir-xyz"
            os.environ["USERPROFILE"] = "/nonexistent-home-dir-xyz"
            os.environ["MINIMAX_API_KEY"] = "env-only"
            os.environ.pop("MINIMAX_API_KEYS", None)
            pool = lc._minimax_key_pool()
        finally:
            if old_home is not None:
                os.environ["HOME"] = old_home
            if old_environ is not None:
                os.environ["USERPROFILE"] = old_environ
        self.assertIn("env-only", pool)


class TestClientConstruction(unittest.TestCase):
    def test_volcano_client_uses_single_key(self):
        """火山端点不接 MiniMax 的 key 池。"""
        cli = lc.OpenAICompatibleClient(model="m", api_key="ark-key",
                                        base_url="https://ark.cn-beijing.volces.com/api/v3")
        self.assertEqual(cli.api_keys, ["ark-key"])
        self.assertNotIsInstance(cli.client, lc.RotatingOpenAI)

    def test_minimax_client_gets_rotating_pool(self):
        cli = lc.OpenAICompatibleClient(model="m", api_key="k1",
                                        base_url="https://api.minimaxi.com/v1")
        self.assertGreaterEqual(len(cli.api_keys), 2)
        self.assertEqual(cli.api_keys[0], "k1")
        self.assertIsInstance(cli.client, lc.RotatingOpenAI)

    def test_explicit_api_keys_are_respected(self):
        cli = lc.OpenAICompatibleClient(model="m", api_keys=["a", "b"],
                                        base_url="https://api.minimaxi.com/v1")
        self.assertEqual(cli.api_keys[:2], ["a", "b"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
