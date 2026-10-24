"""
IC 抄底信号：2026-10-23 三条新规则的单元测试。

跑法（仓库根目录）：
    python -m unittest discover -s tests -v
    python tests/test_ic_signal.py

全部用**合成数据**，不联网、不推钉钉、不写真实状态文件。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO, REPO / "jobs", REPO / "datalayer"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np
import pandas as pd

import ic_signal_monitor as icm


def make_df(dates, spot=None, qvix=None, margin=None, fut=None) -> pd.DataFrame:
    """按给定日期造一张最小可用的行情表（date/spot/fut/qvix/margin）。"""
    n = len(dates)
    spot = spot if spot is not None else np.linspace(6000, 5500, n)
    qvix = qvix if qvix is not None else np.linspace(30, 25, n)
    margin = margin if margin is not None else np.linspace(13000, 12000, n)
    fut = fut if fut is not None else np.array(spot) - 100
    return pd.DataFrame({
        "date": pd.to_datetime(dates),
        "spot": np.asarray(spot, dtype="float64"),
        "fut": np.asarray(fut, dtype="float64"),
        "qvix": np.asarray(qvix, dtype="float64"),
        "margin": np.asarray(margin, dtype="float64"),
    })


def business_days(start: str, n: int):
    """连续 n 个工作日（不含节假日，仅用于构造测试数据）。"""
    return pd.bdate_range(start=start, periods=n).tolist()


class TestQvixGapGuard(unittest.TestCase):
    """规则 1：QVIX 窗口缺日 → 动量无效（不判定），而不是错位成别的一天。"""

    def test_no_gap_computes_momentum(self):
        s = pd.Series([100.0, 101, 102, 103, 104, 105.0])
        out = icm._mom_with_gap_guard(s, 5)
        self.assertAlmostEqual(out.iloc[5], 105.0 / 100.0 - 1.0, places=9)

    def test_gap_inside_window_invalidates(self):
        # 第 2 行缺值 → 第 5 行的窗口不完整 → NaN
        s = pd.Series([100.0, np.nan, 102, 103, 104, 105.0])
        out = icm._mom_with_gap_guard(s, 5)
        self.assertTrue(np.isnan(out.iloc[5]))

    def test_gap_at_baseline_invalidates(self):
        # 基准日（第 0 行）缺 → NaN
        s = pd.Series([np.nan, 101, 102, 103, 104, 105.0])
        out = icm._mom_with_gap_guard(s, 5)
        self.assertTrue(np.isnan(out.iloc[5]))

    def test_pct_change_would_have_been_wrong(self):
        """对照：老口径（位置位移）在同样缺口下会算出**错误**的动量。"""
        s = pd.Series([100.0, np.nan, 102, 103, 104, 105.0])
        old = s.pct_change(5, fill_method=None)
        new = icm._mom_with_gap_guard(s, 5)
        self.assertFalse(pd.isna(old.iloc[5]))     # 老口径给了一个数（错的）
        self.assertTrue(pd.isna(new.iloc[5]))       # 新口径拒绝判定

    def test_prepare_signals_uses_guard(self):
        dates = business_days("2026-01-05", 40)
        qvix = np.linspace(30, 26, 40)
        qvix[30] = np.nan          # 制造缺口
        df = make_df(dates, qvix=qvix)
        out = icm.prepare_signals(df)
        # 缺口落在 idx30 → 窗口含 idx30 的行（30~35）动量无效；
        # 窗口不再含缺口的行（36 起）恢复有效
        self.assertTrue(out["qvix_mom_5d"].iloc[30:36].isna().all())
        self.assertFalse(out["qvix_mom_5d"].iloc[36:].isna().any())
        # 前 5 行没有基准（前 4 行本身 NaN 是正常的），5~29 应全部有效
        self.assertFalse(out["qvix_mom_5d"].iloc[5:30].isna().any())


class TestPreHolidayDeferral(unittest.TestCase):
    """规则 2：长假前夜 → 不判定，顺延到节后第一个交易日。"""

    def setUp(self):
        self._cal = mock.patch.object(icm, "_trade_calendar", return_value={
            pd.Timestamp("2026-09-24").date(),     # 节前最后交易日
            pd.Timestamp("2026-09-28").date(),     # 节后首日
            pd.Timestamp("2026-09-29").date(),
            pd.Timestamp("2026-10-09").date(),
        })
        self._cal.start()
        # 清掉模块级缓存（交易日历 / 长假前夜集合），保证用打桩的日历
        icm._TRADE_CAL = None
        icm._TRADE_CAL_TS = 0.0
        icm._PRE_HOLIDAY = None
        icm._PRE_HOLIDAY_TS = 0.0
        self.addCleanup(self._cal.stop)

    def tearDown(self):
        self._cal.stop()

    def test_pre_holiday_night_detected(self):
        self.assertTrue(icm.is_pre_long_holiday(pd.Timestamp("2026-09-24")))

    def test_normal_day_not_deferred(self):
        self.assertFalse(icm.is_pre_long_holiday(pd.Timestamp("2026-09-28")))
        self.assertFalse(icm.is_pre_long_holiday(pd.Timestamp("2026-10-09")))

    def test_calendar_missing_degrades_to_no_defer(self):
        with mock.patch.object(icm, "_trade_calendar", return_value=set()):
            self.assertFalse(icm.is_pre_long_holiday(pd.Timestamp("2026-09-24")))


class TestSignalStrength(unittest.TestCase):
    """规则 3：融资强度分级（≤-3% 强 / >-3% 弱），不设硬门槛。"""

    def test_strong(self):
        level, why = icm.signal_strength({"margin_mom20": -0.0647})
        self.assertEqual(level, "强")
        self.assertIn("-6.47%", why)

    def test_weak(self):
        level, why = icm.signal_strength({"margin_mom20": -0.0128})
        self.assertEqual(level, "弱")
        self.assertIn("趋势性", why)

    def test_missing(self):
        level, _ = icm.signal_strength({"margin_mom20": float("nan")})
        self.assertEqual(level, "未知")


class TestRunOnceBehaviour(unittest.TestCase):
    """run_once 的三条关键行为：顺延跳过、缺口不触发、顺延备注透传、去重。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "state.json"
        self.hist_path = Path(self.tmp.name) / "history.json"
        self.sent: list = []

        patches = [
            mock.patch.object(icm, "STATE_PATH", self.state_path),
            mock.patch.object(icm, "SIGNAL_HISTORY", self.hist_path),
            mock.patch.object(icm, "LATEST_SIGNAL", Path(self.tmp.name) / "latest.json"),
            mock.patch.object(icm, "push_bot_message",
                              side_effect=lambda text, title="t": self.sent.append((text, title)) or True),
            mock.patch.object(icm, "_trade_calendar", return_value={
                pd.Timestamp("2026-09-24").date(),
                pd.Timestamp("2026-09-28").date(),
                pd.Timestamp("2026-09-29").date(),
            }),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        # 清掉模块级缓存（交易日历 / 长假前夜集合），保证用打桩的日历
        icm._TRADE_CAL = None
        icm._TRADE_CAL_TS = 0.0
        icm._PRE_HOLIDAY = None
        icm._PRE_HOLIDAY_TS = 0.0

    def tearDown(self):
        self.tmp.cleanup()

    def _df_triggers(self, last_date="2026-09-28"):
        """造一段会触发的数据（RSI 低 / QVIX 5日跌 / vol>20% / 融资降）。

        参数是标定过的：drift=-1.0%、noise=1.6% → RSI≈11、vol60≈23%、
        QVIX 末段从 40 砸到 24（5日动量 -32%）、融资 20 日 -3.8%。
        """
        rng = np.random.default_rng(7)
        dates = pd.bdate_range(start="2026-06-01", end="2026-09-28").tolist()
        n = len(dates)
        spot = 7000 * np.cumprod(1 + rng.normal(-0.010, 0.016, n))
        qvix = np.concatenate([np.full(n - 8, 30.0), np.linspace(40, 24, 8)])
        margin = np.linspace(14000, 12000, n)
        df = make_df(dates, spot=spot, qvix=qvix, margin=margin)
        if last_date:
            df = df[df["date"] <= pd.Timestamp(last_date)].reset_index(drop=True)
        return df

    def test_deferral_skips_judgement(self):
        """最新数据日是长假前夜 → 不判定、不推、状态记下顺延。"""
        df = self._df_triggers(last_date="2026-09-24")
        self.assertEqual(str(df["date"].iloc[-1].date()), "2026-09-24")
        with mock.patch.object(icm, "load_latest_data", return_value=df):
            res = icm.run_once(dry_run=False)
        self.assertEqual(res.get("skipped"), "pre-holiday-deferred")
        self.assertEqual(res.get("deferred_date"), "2026-09-24")
        self.assertEqual(self.sent, [])            # 什么都没推
        self.assertEqual(icm.load_state().get("deferred_date"), "2026-09-24")

    def test_gap_blocks_trigger(self):
        """QVIX 窗口缺日 → 不触发（data_ok=False）。"""
        df = self._df_triggers()
        df["qvix"].iloc[-3] = np.nan               # 制造窗口缺口
        with mock.patch.object(icm, "load_latest_data", return_value=df):
            res = icm.run_once(dry_run=False)
        self.assertFalse(res["bottom_triggered"])
        self.assertFalse(res["data_ok"])
        checked = icm.check_bottom_signal(icm.prepare_signals(df))
        self.assertIn("QVIX窗口缺日", checked["stale_reason"])

    def test_trigger_pushes_once_and_has_strength(self):
        """触发 → 推一条；消息里带强度分级；再跑一次不重复推。"""
        df = self._df_triggers()
        with mock.patch.object(icm, "load_latest_data", return_value=df):
            res1 = icm.run_once(dry_run=False)
            res2 = icm.run_once(dry_run=False)
        self.assertTrue(res1["bottom_triggered"])
        self.assertEqual(len(self.sent), 1)         # 第二次被历史去重挡住
        text = self.sent[0][0]
        self.assertIn("信号强度", text)
        self.assertIn("强", text)
        self.assertNotIn("建议: 加仓", text)        # 只做分析提示，不给具体买卖指令
        self.assertEqual(res2.get("skipped"), "signal-already-sent")

    def test_deferred_note_appears_on_next_judgement(self):
        """节前触发被顺延后，下一轮（节后首日）判定要带上顺延备注。"""
        pre = self._df_triggers(last_date="2026-09-24")
        post = self._df_triggers(last_date="2026-09-28")
        with mock.patch.object(icm, "load_latest_data", return_value=pre):
            icm.run_once(dry_run=False)
        self.assertEqual(self.sent, [])
        with mock.patch.object(icm, "load_latest_data", return_value=post):
            icm.run_once(dry_run=False)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("已顺延", self.sent[0][0])


class _FrozenDatetime(datetime):
    """把模块里的 datetime.now() 钉死，用来模拟周末。"""

    frozen = datetime(2026, 10, 25, 8, 50)      # 周日

    @classmethod
    def now(cls, tz=None):
        return cls.frozen


class TestWeekendBehaviour(unittest.TestCase):
    """2026-10-24：IC 监控周末也跑（判最新交易日数据 + 推「非交易日回顾」）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "state.json"
        self.sent: list = []
        patches = [
            mock.patch.object(icm, "STATE_PATH", self.state_path),
            mock.patch.object(icm, "SIGNAL_HISTORY", Path(self.tmp.name) / "history.json"),
            mock.patch.object(icm, "LATEST_SIGNAL", Path(self.tmp.name) / "latest.json"),
            mock.patch.object(icm, "push_bot_message",
                              side_effect=lambda text, title="t": self.sent.append((text, title)) or True),
            mock.patch.object(icm, "_trade_calendar", return_value=set()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        icm._TRADE_CAL = None
        icm._TRADE_CAL_TS = 0.0
        icm._PRE_HOLIDAY = None
        icm._PRE_HOLIDAY_TS = 0.0

    def tearDown(self):
        self.tmp.cleanup()

    def _df_flat(self):
        """一段**不触发**任何信号的数据（涨势 + QVIX 走高 + 融资走高）。"""
        dates = pd.bdate_range(start="2026-06-01", end="2026-10-23").tolist()
        n = len(dates)
        return make_df(dates,
                       spot=np.linspace(5000, 6000, n),
                       qvix=np.linspace(20, 30, n),
                       margin=np.linspace(12000, 13000, n))

    def test_weekend_runs_and_pushes_review(self):
        """周末不再 skipped=weekend，而是照常判定 + 推「非交易日回顾」。"""
        df = self._df_flat()
        with mock.patch.object(icm, "datetime", _FrozenDatetime):
            with mock.patch.object(icm, "load_latest_data", return_value=df):
                res = icm.run_once(dry_run=False)
        self.assertNotEqual(res.get("skipped"), "weekend")
        self.assertEqual(res["sent"], ["heartbeat"])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("非交易日回顾", self.sent[0][0])
        self.assertEqual(self.sent[0][1], "IC 非交易日回顾")

    def test_weekend_same_data_does_not_spam(self):
        """同一份数据（周日再跑）→ 跳过，不重复推回顾。"""
        df = self._df_flat()
        with mock.patch.object(icm, "datetime", _FrozenDatetime):
            with mock.patch.object(icm, "load_latest_data", return_value=df):
                res1 = icm.run_once(dry_run=False)
                res2 = icm.run_once(dry_run=False)
        self.assertEqual(res1["sent"], ["heartbeat"])
        self.assertEqual(res2.get("skipped"), "no-new-data")
        self.assertEqual(len(self.sent), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
