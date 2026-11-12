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

    def test_missing_qvix_does_not_zero_out_other_conditions(self):
        """2026-10-25 修：守门**按指标分开算**。

        老写法：只要有任一数据缺失，qvix_ok / margin_ok 一起打成 False →
        QVIX 缺一天，融资明明 -7.25% < 0 也被显示成 ❌（用户以为条件不满足）。
        现在：每项只受自己那侧的数据影响。

        2026-11-09：`data_ok=False` 仍表示「有缺口」，但**不再是整轮拒判**
        —— 其余三项都满足时升级成「待确认」，因为信号本身确实没触发。
        """
        df = self._df_triggers()
        df.loc[df.index[-1], "qvix"] = np.nan            # 只缺 QVIX
        checked = icm.check_bottom_signal(icm.prepare_signals(df))
        self.assertFalse(checked["qvix_ok"])             # QVIX 那项不成立
        self.assertTrue(checked["margin_ok"])            # 融资那项照常判（-3.8% < 0）
        self.assertFalse(checked["data_ok"])             # 有缺口
        self.assertFalse(checked["triggered"])
        self.assertIn("QVIX", checked["stale_reason"])
        self.assertFalse(checked["hard_bad"])            # 不是「真的没法判」
        self.assertTrue(checked["pending_confirm"])      # 其余条件已满足 → 待确认
        self.assertEqual(icm.bottom_state_text(checked),
                         "⚠️ 待确认（QVIX 缺，其余条件已满足）")

    def test_self_qvix_fallback_never_triggers(self):
        """2026-10-26：自算 QVIX 只进 qvix_self 列**仅作参考**，不得影响判定。

        官方 QVIX 缺最新日 + 自算值满窗 → 结果仍必须是 data_ok=False / triggered=False，
        且 qvix_self 那份「看起来满足条件」的动量不能把 qvix_ok 顶成 True。
        """
        df = self._df_triggers()
        df.loc[df.index[-1], "qvix"] = np.nan
        # 自算序列：5 日大幅回落（如果被当成判据，抄底就误触发了）
        n = len(df)
        df["qvix_self"] = np.concatenate([np.full(n - 6, 30.0), np.linspace(40, 24, 6)])
        checked = icm.check_bottom_signal(icm.prepare_signals(df))
        self.assertFalse(checked["qvix_ok"])
        self.assertFalse(checked["data_ok"])
        self.assertFalse(checked["triggered"])
        # 但参考值要能拿到（卡片上要显示）
        self.assertAlmostEqual(checked["qvix_self_val"], 24.0, places=6)
        self.assertLess(checked["qvix_self_mom_5d"], -0.03)

    def test_missing_margin_does_not_zero_out_qvix(self):
        """反向：融资缺 → 融资那项不成立，QVIX 项照常判。"""
        df = self._df_triggers()
        df.loc[df.index[-1], "margin"] = np.nan
        checked = icm.check_bottom_signal(icm.prepare_signals(df))
        self.assertFalse(checked["margin_ok"])
        self.assertTrue(checked["qvix_ok"])
        self.assertFalse(checked["data_ok"])

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
        """QVIX 窗口缺日 → 不触发；但其余条件照常判、状态不是"整轮拒判"。"""
        df = self._df_triggers()
        df["qvix"].iloc[-3] = np.nan               # 制造窗口缺口
        with mock.patch.object(icm, "load_latest_data", return_value=df):
            res = icm.run_once(dry_run=False)
        self.assertFalse(res["bottom_triggered"])
        self.assertFalse(res["data_ok"])
        self.assertTrue(res["pending_confirm"])
        self.assertEqual(res["missing_qvix_dates"], [df["date"].iloc[-3].strftime("%Y-%m-%d")])
        checked = icm.check_bottom_signal(icm.prepare_signals(df))
        self.assertIn("QVIX窗口缺日", checked["stale_reason"])
        self.assertFalse(checked["hard_bad"])
        # 心跳文案要把缺的那天写清楚，并说明「条件2 无法确认」
        text = self.sent[0][0]
        self.assertIn("待确认", text)
        self.assertIn(df["date"].iloc[-3].strftime("%Y-%m-%d"), text)
        self.assertNotIn("拒判", text)

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
        _FrozenDatetime.frozen = datetime(2026, 10, 25, 8, 50)   # 周日（用例会改它）
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

    def test_weekend_same_day_runs_once(self):
        """同一天重复跑 → 只推一条（按运行日去重）。"""
        df = self._df_flat()
        with mock.patch.object(icm, "datetime", _FrozenDatetime):
            with mock.patch.object(icm, "load_latest_data", return_value=df):
                res1 = icm.run_once(dry_run=False)
                res2 = icm.run_once(dry_run=False)
        self.assertEqual(res1["sent"], ["heartbeat"])
        self.assertEqual(res2.get("skipped"), "heartbeat-already-sent")
        self.assertEqual(len(self.sent), 1)

    def test_next_day_same_data_still_pushes(self):
        """2026-10-25 改：**每天早上必有一条**。

        同一份收盘数据（周末没新行情）隔天再跑，也要推一条 —— 老的
        「数据日没变就跳过」会把周日 08:50 这一轮整个吞掉（用户反馈的漏发）。
        """
        df = self._df_flat()
        _FrozenDatetime.frozen = datetime(2026, 10, 25, 8, 50)      # 周日
        with mock.patch.object(icm, "datetime", _FrozenDatetime):
            with mock.patch.object(icm, "load_latest_data", return_value=df):
                res1 = icm.run_once(dry_run=False)
                _FrozenDatetime.frozen = datetime(2026, 10, 26, 8, 50)  # 周一
                res2 = icm.run_once(dry_run=False)
        self.assertEqual(res1["sent"], ["heartbeat"])
        self.assertEqual(res2["sent"], ["heartbeat"])     # 没被"无新数据"吞掉
        self.assertEqual(len(self.sent), 2)
        # 第二条仍标明结论基于哪个交易日
        self.assertIn(df["date"].iloc[-1].strftime("%Y-%m-%d"), self.sent[1][0])


class TestDataLagNote(unittest.TestCase):
    """2026-10-25：行情源比交易日历慢时，消息里必须明说结论基于哪一天。"""

    def test_note_when_source_lags(self):
        cal = {datetime(2026, 10, 22).date(), datetime(2026, 10, 23).date()}
        with mock.patch.object(icm, "_trade_calendar", return_value=cal):
            note = icm.data_lag_note("2026-10-22", now=datetime(2026, 10, 25, 8, 50))
        self.assertIn("行情源滞后", note)
        self.assertIn("2026-10-23", note)

    def test_no_note_when_up_to_date(self):
        cal = {datetime(2026, 10, 22).date(), datetime(2026, 10, 23).date()}
        with mock.patch.object(icm, "_trade_calendar", return_value=cal):
            note = icm.data_lag_note("2026-10-23", now=datetime(2026, 10, 25, 8, 50))
        self.assertEqual(note, "")


class TestStatusDisplay(unittest.TestCase):
    """2026-10-25：缺数据要显示成「⚠️缺」，不能混成 ❌（用户被误导过）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sent: list = []
        patches = [
            mock.patch.object(icm, "STATE_PATH", Path(self.tmp.name) / "state.json"),
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

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_value_shows_not_satisfied(self):
        """QVIX 缺失 → 显示「缺 ⚠️」；融资有值且满足 → 显示「✅」。"""
        dates = pd.bdate_range(start="2026-06-01", end="2026-10-23").tolist()
        n = len(dates)
        df = make_df(dates,
                     spot=np.linspace(7000, 5500, n),
                     qvix=np.linspace(30, 24, n),
                     margin=np.linspace(14000, 12000, n))
        df.loc[df.index[-1], "qvix"] = np.nan
        sig = icm.prepare_signals(df)
        bottom = icm.check_bottom_signal(sig)
        top = icm.check_top_signal(sig)
        icm.notify_dingtalk_heartbeat(bottom, top, weekend=True)
        text = self.sent[0][0]
        self.assertIn("QVIX 5日 = ", text)
        # 2026-10-26：最新日缺 QVIX 时不再只显示一个「缺」，而是把上一个有效日的
        # 动量摆出来并标明口径（图标仍是 ⚠️缺）
        self.assertIn("截至", text)
        self.assertIn("⚠️缺", text)
        self.assertIn("融资20日 = ", text)
        self.assertNotIn("nan%", text)
        # 2026-11-09：缺 QVIX 不再把整张卡打成「拒判」——其余条件照常判，
        # 状态改写成「未触发（QVIX 缺，条件2 无法确认）」，并写清缺的是哪一天。
        self.assertIn("无法确认", text)
        self.assertIn("2026-10-23", text)
        self.assertNotIn("拒判", text)

    def test_all_other_conditions_pass_shows_pending(self):
        """其余三项全满足、只差 QVIX → 状态「⚠️待确认」，不是静默的「未触发」。"""
        dates = pd.bdate_range(start="2026-06-01", end="2026-10-23").tolist()
        n = len(dates)
        rng = np.random.default_rng(11)
        spot = 7000 * np.cumprod(1 + rng.normal(-0.006, 0.02, n))
        df = make_df(dates,
                     spot=spot,
                     qvix=np.linspace(30, 24, n),
                     margin=np.linspace(14000, 12000, n))
        df.loc[df.index[-2], "qvix"] = np.nan      # 窗口里缺一天
        sig = icm.prepare_signals(df)
        bottom = icm.check_bottom_signal(sig)
        top = icm.check_top_signal(sig)
        self.assertTrue(bottom["pending_confirm"])
        self.assertEqual(icm.bottom_state_text(bottom),
                         "⚠️ 待确认（QVIX 缺，其余条件已满足）")
        self.sent.clear()
        icm.notify_dingtalk_heartbeat(bottom, top)
        self.assertEqual(self.sent[0][1], "IC 待确认信号")


if __name__ == "__main__":
    unittest.main(verbosity=2)
