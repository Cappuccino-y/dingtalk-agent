"""QVIX 日线抓取的韧性（datalayer/akshare_helper.py）单元测试。

背景（2026-11-02）：IC 补判 09:20 那轮，optbbs 的日线 CSV（约 900KB）下到一半
被截断 → `http.client.IncompleteRead` → 整个 IC 作业 exit 1 → 推了一张假告警，
3 分钟后调度器重试又成功了。

这里锁定三件事：
1. 一次失败会重试，重试成功就当成功；
2. 重试全败时退回**本地全量快照**，不再把作业打死；
3. 连快照都没有才抛异常（由上层明确报缺数据）。

跑法（仓库根目录）：python -m unittest tests.test_qvix_fetch -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO, REPO / "datalayer"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd

import akshare_helper as ah
import qvix_store as qs

# akshare_helper 是按 `datalayer.qvix_store` 导入的（和生产环境一致）→
# 那是另一个模块实例，CACHE_DIR 得一起改，否则快照会落到真实 var/qvix/。
import datalayer.qvix_store as dqs


def _daily_df() -> pd.DataFrame:
    return pd.DataFrame({
        "date": pd.to_datetime(["2026-10-28", "2026-10-29", "2026-10-30"]),
        "close": [30.1, 28.9, 28.94],
    })


class TestFetchQvixResilience(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_dirs = [(m, m.CACHE_DIR) for m in (qs, dqs)]
        for m in (qs, dqs):
            m.CACHE_DIR = Path(self.tmp.name)
        # 别真睡
        self._sleep = mock.patch.object(ah.time, "sleep", return_value=None)
        self._sleep.start()

    def tearDown(self):
        self._sleep.stop()
        for m, old in self._old_dirs:
            m.CACHE_DIR = old
        self.tmp.cleanup()

    def test_retry_recovers_from_truncated_download(self):
        """第 1 次下载被截断（IncompleteRead），第 2 次完整 → 正常返回。"""
        calls = {"n": 0}

        def flaky_download(**_kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("下载不完整（213215/923605 bytes）")
            return b"x" * ah.QVIX_MIN_BYTES

        with mock.patch.object(ah, "_download_optbbs_daily", side_effect=flaky_download), \
             mock.patch.object(ah, "_qvix_df_from_bytes", return_value=_daily_df()):
            df = ah.fetch_qvix("500etf", use_cache=False)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(len(df), 3)
        self.assertAlmostEqual(df["close"].iloc[-1], 28.94)

    def test_falls_back_to_local_full_snapshot(self):
        """重试全败 → 用本地全量快照，作业不崩。"""
        dqs.save_full("500etf", _daily_df())

        def always_fail():
            raise ConnectionError("IncompleteRead")

        with mock.patch.object(ah, "_download_optbbs_daily", side_effect=always_fail), \
             mock.patch.dict(ah.QVIX_FETCHERS, {"500etf": always_fail}), \
             mock.patch.object(ah, "QVIX_FETCH_ATTEMPTS", 2):
            df = ah.fetch_qvix("500etf", use_cache=False)
        self.assertEqual(len(df), 3)
        self.assertAlmostEqual(df["close"].iloc[0], 30.1)

    def test_uses_daily_snapshot_when_full_snapshot_missing(self):
        """源站挂 + 没有整段快照，但 15:05 收盘快照有 → 只用那一天也认。"""
        dqs._save_cached("500etf", pd.DataFrame({
            "date": pd.to_datetime(["2026-10-30"]),
            "close": [32.0],
            "source": ["intraday@15:00:00"],
            "captured_at": [""],
        }))

        def always_fail():
            raise ConnectionError("IncompleteRead")

        with mock.patch.object(ah, "_download_optbbs_daily", side_effect=always_fail), \
             mock.patch.dict(ah.QVIX_FETCHERS, {"500etf": always_fail}), \
             mock.patch.object(ah, "QVIX_FETCH_ATTEMPTS", 1):
            df = ah.fetch_qvix("500etf", use_cache=False)
        self.assertEqual(len(df), 1)
        self.assertAlmostEqual(float(df["close"].iloc[-1]), 32.0)

    def test_raises_when_no_source_and_no_snapshot(self):
        """源站挂 + 无快照 → 抛异常（上层按「QVIX 缺」处理，不静默给结论）。"""

        def always_fail():
            raise ConnectionError("IncompleteRead")

        with mock.patch.object(ah, "_download_optbbs_daily", side_effect=always_fail), \
             mock.patch.dict(ah.QVIX_FETCHERS, {"500etf": always_fail}), \
             mock.patch.object(ah, "QVIX_FETCH_ATTEMPTS", 2):
            with self.assertRaises(RuntimeError):
                ah.fetch_qvix("500etf", use_cache=False)

    def test_success_refreshes_full_snapshot(self):
        """源站成功时顺手把整段历史落盘，供以后兜底。"""
        with mock.patch.object(ah, "_download_optbbs_daily",
                               return_value=b"x" * ah.QVIX_MIN_BYTES), \
             mock.patch.object(ah, "_qvix_df_from_bytes", return_value=_daily_df()):
            ah.fetch_qvix("500etf", use_cache=False)
        cached = dqs.load_full("500etf")
        self.assertEqual(len(cached), 3)

    def test_verified_value_beats_bad_source_row(self):
        """源站补出来的 11-02 = 27.51 是错算 → 用核对值 35.62（2026-11-04 实况）。"""
        src = pd.DataFrame({
            "date": pd.to_datetime(["2026-10-30", "2026-11-01", "2026-11-02"]),
            "close": [35.60, 37.42, 27.51],       # 11-01 是周日（源站日期写错）
        })
        dqs.repair_day("500etf", "2026-11-02", 35.62, reason="双源核对")
        with mock.patch.object(ah, "_download_optbbs_daily",
                               return_value=b"x" * ah.QVIX_MIN_BYTES), \
             mock.patch.object(ah, "_qvix_df_from_bytes", return_value=src):
            df = ah.fetch_qvix("500etf")
        by_date = dict(zip(df["date"].dt.strftime("%Y-%m-%d"), df["close"]))
        self.assertNotIn("2026-11-01", by_date)          # 周末行被剔除
        self.assertAlmostEqual(by_date["2026-11-02"], 35.62)
        full = dqs.load_full("500etf")                   # 快照里也不能存错值
        self.assertNotIn(27.51, [round(float(v), 2) for v in full["close"]])

    def test_duplicate_source_day_is_deduped(self):
        """源站给同一天吐两行（11-02 的 27.51 与 33.81）→ 只留一行。

        2026-11-09：以前完全没去重，重复行进 df 后，按位置位移的 5 日动量窗口
        会整体错位一格（"5 天前"落到 4 天或 6 天前），判定口径再严也没用。
        """
        src = pd.DataFrame({
            "date": pd.to_datetime(["2026-10-30", "2026-11-02", "2026-11-02"]),
            "close": [35.60, 27.51, 33.81],
        })
        dqs.repair_day("500etf", "2026-11-02", 35.62, reason="双源核对")
        with mock.patch.object(ah, "_download_optbbs_daily",
                               return_value=b"x" * ah.QVIX_MIN_BYTES), \
             mock.patch.object(ah, "_qvix_df_from_bytes", return_value=src):
            df = ah.fetch_qvix("500etf")
        self.assertEqual(len(df), 2)                     # 10-30 + 11-02
        self.assertEqual(len(df["date"].unique()), 2)    # 没有重复日期
        by_date = dict(zip(df["date"].dt.strftime("%Y-%m-%d"), df["close"]))
        self.assertAlmostEqual(by_date["2026-11-02"], 35.62)


class TestOptbbsDownload(unittest.TestCase):
    """下载器本身：超时/截断都要能识别出来，别把半截 CSV 当数据用。"""

    def _fake_resp(self, content: bytes, length: int | None = None):
        resp = mock.Mock()
        resp.headers = {"Content-Length": str(length if length is not None else len(content))}
        resp.raise_for_status.return_value = None
        resp.iter_content.return_value = [content]
        resp.close.return_value = None
        return resp

    def _patch_session(self, resp):
        patcher = mock.patch("requests.Session")
        session = patcher.start()
        session.return_value.get.return_value = resp
        self.addCleanup(patcher.stop)
        return session

    def test_short_read_is_rejected(self):
        """Content-Length 对不上 → 报错（而不是让 pandas 去解析半截文件）。"""
        resp = self._fake_resp(b"x" * 1000, length=923605)
        self._patch_session(resp)
        with self.assertRaises(IOError):
            ah._download_optbbs_daily()

    def test_tiny_body_is_rejected(self):
        resp = self._fake_resp(b"date,qvix\n", length=11)
        self._patch_session(resp)
        with self.assertRaises(IOError):
            ah._download_optbbs_daily()

    def test_complete_body_passes_with_bounded_timeouts(self):
        body = b"x" * ah.QVIX_MIN_BYTES
        session = self._patch_session(self._fake_resp(body))
        data = ah._download_optbbs_daily()
        self.assertEqual(len(data), ah.QVIX_MIN_BYTES)
        kwargs = session.return_value.get.call_args.kwargs
        self.assertEqual(kwargs["timeout"], (ah.QVIX_CONNECT_TIMEOUT, ah.QVIX_READ_TIMEOUT))
        self.assertTrue(kwargs["stream"])

    def test_total_deadline_aborts_slow_stream(self):
        """源站一直滴数据但极慢 → 总时限到了就放弃，不能干等几分钟。"""
        resp = self._fake_resp(b"x" * ah.QVIX_MIN_BYTES)
        resp.iter_content.return_value = [b"x" * 1000] * 5
        self._patch_session(resp)
        with mock.patch.object(ah.time, "monotonic", side_effect=[0, 0, 1, 2, 3, 9999]):
            with self.assertRaises(IOError):
                ah._download_optbbs_daily()


class TestFullSnapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_dir = qs.CACHE_DIR
        qs.CACHE_DIR = Path(self.tmp.name)

    def tearDown(self):
        qs.CACHE_DIR = self._old_dir
        self.tmp.cleanup()

    def test_shorter_source_does_not_erase_history(self):
        """源站这次只返回一天，也不能把本地已有的历史吃掉。"""
        qs.save_full("500etf", _daily_df())
        qs.save_full("500etf", pd.DataFrame({
            "date": pd.to_datetime(["2026-11-02"]), "close": [27.0]}))
        cached = qs.load_full("500etf")
        self.assertEqual(len(cached), 4)
        self.assertAlmostEqual(float(cached["close"].iloc[-1]), 27.0)

    def test_missing_snapshot_returns_empty(self):
        self.assertTrue(qs.load_full("50etf").empty)


if __name__ == "__main__":
    unittest.main(verbosity=2)
