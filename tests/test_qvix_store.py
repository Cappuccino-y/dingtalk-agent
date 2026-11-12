"""QVIX 本地自建库（datalayer/qvix_store.py）的单元测试。

跑法（仓库根目录）：
    python -m unittest discover -s tests -v

全部用合成数据 + 打桩，不联网、不写 var/。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO, REPO / "datalayer"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np
import pandas as pd

import qvix_store as qs


class TestQvixStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_dir = qs.CACHE_DIR
        qs.CACHE_DIR = Path(self.tmp.name)

    def tearDown(self):
        qs.CACHE_DIR = self._old_dir
        self.tmp.cleanup()

    def test_overlay_fills_gap_without_overriding_source(self):
        """本地库只补空：源站已有的值绝不覆盖，空的才填。"""
        src = pd.DataFrame({
            "date": pd.to_datetime(["2026-10-21", "2026-10-22", "2026-10-23"]),
            "close": [26.99, 29.81, np.nan],
        })
        qs._save_cached("500etf", pd.DataFrame({
            "date": pd.to_datetime(["2026-10-22", "2026-10-23"]),
            "close": [99.0, 28.94],
            "source": ["intraday@15:00:00", "intraday@15:00:00"],
            "captured_at": ["", ""],
        }))
        out = qs.overlay(src, "500etf")
        self.assertAlmostEqual(out.loc[1, "close"], 29.81)   # 源站值保留
        self.assertAlmostEqual(out.loc[2, "close"], 28.94)   # 空缺被补

    def test_overlay_no_cache_is_noop(self):
        src = pd.DataFrame({
            "date": pd.to_datetime(["2026-10-23"]),
            "close": [np.nan],
        })
        out = qs.overlay(src, "500etf")
        self.assertTrue(np.isnan(out.loc[0, "close"]))

    def test_snapshot_takes_last_valid_minute(self):
        """取收盘那一刻（最后一条有效分钟值），不是第一条。"""
        pairs = [("9:30:00", 30.1), ("14:59:00", 28.90), ("15:00:00", 28.94)]
        with mock.patch.object(qs, "_fetch_minute_table", return_value=pairs), \
             mock.patch.object(qs, "_is_trading_day", return_value=True):
            r = qs.snapshot("500etf", for_date=date(2026, 10, 23), allow_intraday=True)
        self.assertTrue(r["ok"])
        self.assertAlmostEqual(r["close"], 28.94)
        cached = qs.load_cached("500etf")
        self.assertEqual(len(cached), 1)
        self.assertAlmostEqual(float(cached.iloc[-1]["close"]), 28.94)

    def test_snapshot_refuses_empty_minute_file(self):
        """源站当天也没算出来 → 不落库，返回原因（不写假数据）。"""
        with mock.patch.object(qs, "_fetch_minute_table", return_value=[]), \
             mock.patch.object(qs, "_is_trading_day", return_value=True):
            r = qs.snapshot("500etf", for_date=date(2026, 10, 23), allow_intraday=True)
        self.assertFalse(r["ok"])
        self.assertIn("无有效值", r["reason"])
        self.assertTrue(qs.load_cached("500etf").empty)

    # ---------------- 2026-11-03：源站「不更新但不报错」的守门 ----------------
    PAIRS = [("9:30:00", 33.10), ("14:59:00", 35.60), ("15:00:00", 35.69)]

    def _seed_prev(self, close: float, fp: str = "") -> None:
        qs._save_cached("500etf", pd.DataFrame({
            "date": pd.to_datetime(["2026-10-30"]),
            "close": [close],
            "source": ["intraday@15:00:00"],
            "captured_at": [""],
            "fp": [fp],
        }))

    def test_snapshot_refuses_when_minute_series_repeats_previous_day(self):
        """整份分钟文件与上一交易日逐点相同 = 源站没更新 → 拒落库（11-02 的假 35.69）。"""
        fp = qs._fingerprint(self.PAIRS)
        self._seed_prev(35.69, fp)
        with mock.patch.object(qs, "_fetch_minute_table", return_value=list(self.PAIRS)), \
             mock.patch.object(qs, "_is_trading_day", return_value=True):
            r = qs.snapshot("500etf", for_date=date(2026, 11, 2), allow_intraday=True)
        self.assertFalse(r["ok"])
        self.assertIn("源站未更新", r["reason"])
        cached = qs.load_cached("500etf")
        self.assertEqual(len(cached), 1)                       # 只剩 10-30
        self.assertNotIn("2026-11-02", cached["date"].dt.strftime("%Y-%m-%d").tolist())

    def test_snapshot_accepts_same_close_with_different_path(self):
        """收盘值巧合相同但盘中路径不同 = 真数据，照常落库（不误杀，历史上有 19 次）。"""
        self._seed_prev(35.69, "221|30.00|35.69|33.10")
        with mock.patch.object(qs, "_fetch_minute_table", return_value=list(self.PAIRS)), \
             mock.patch.object(qs, "_is_trading_day", return_value=True):
            r = qs.snapshot("500etf", for_date=date(2026, 11, 2), allow_intraday=True)
        self.assertTrue(r["ok"])
        self.assertFalse(r["suspect"])
        self.assertAlmostEqual(float(qs.load_cached("500etf").iloc[-1]["close"]), 35.69)

    def test_snapshot_marks_suspect_when_prev_has_no_fingerprint(self):
        """上一交易日是老版本写的（无指纹）→ 只标 suspect，仍落库，但默认读不到。"""
        self._seed_prev(35.69, "")
        with mock.patch.object(qs, "_fetch_minute_table", return_value=list(self.PAIRS)), \
             mock.patch.object(qs, "_is_trading_day", return_value=True):
            r = qs.snapshot("500etf", for_date=date(2026, 11, 2), allow_intraday=True)
        self.assertTrue(r["ok"])
        self.assertTrue(r["suspect"])
        self.assertEqual(len(qs.load_cached("500etf")), 1)
        self.assertEqual(len(qs.load_cached("500etf", include_suspect=True)), 2)

    def test_suspect_row_is_not_used_as_reference(self):
        """suspect 行不能当上一交易日基准（11-02 那个假 35.69 不能拿来比）。"""
        qs._save_cached("500etf", pd.DataFrame({
            "date": pd.to_datetime(["2026-10-30", "2026-11-02"]),
            "close": [35.60, 35.69],
            "source": ["intraday@15:00:00", "intraday@15:00:35"],
            "captured_at": ["", ""],
            "fp": ["221|30.00|35.60|33.10", ""],
            "suspect": ["", "1"],
        }))
        prev, trusted = qs._prev_row("500etf", date(2026, 11, 3))
        self.assertIsNotNone(prev)
        self.assertTrue(trusted)
        self.assertAlmostEqual(float(prev["close"]), 35.60)
        self.assertEqual(prev["date"].strftime("%Y-%m-%d"), "2026-10-30")

    def test_overlay_skips_suspect_rows(self):
        """suspect 行不得用来补洞 —— 宁可继续缺、让守门拒判。"""
        src = pd.DataFrame({"date": pd.to_datetime(["2026-11-02"]), "close": [np.nan]})
        qs._save_cached("500etf", pd.DataFrame({
            "date": pd.to_datetime(["2026-11-02"]),
            "close": [35.69],
            "source": ["intraday@15:00:35"],
            "captured_at": [""],
            "fp": [""],
            "suspect": ["1"],
        }))
        out = qs.overlay(src, "500etf")
        self.assertTrue(np.isnan(out.loc[0, "close"]))

    # ---------------- 2026-11-04：源站补行补出错值 ----------------
    def test_overlay_appends_days_source_has_not_backfilled(self):
        """源站还停在 11-02、本地有 11-03 的收盘快照 → 要把 11-03 整行补进来。

        只填 NaN 不够：日期行不存在时 merge 后仍是空，5 日动量永远算不出来。
        """
        src = pd.DataFrame({
            "date": pd.to_datetime(["2026-11-02"]), "close": [35.62]})
        qs._save_cached("500etf", pd.DataFrame({
            "date": pd.to_datetime(["2026-11-03"]),
            "close": [37.18],
            "source": ["intraday@15:00:34"],
            "captured_at": [""],
        }))
        out = qs.overlay(src, "500etf")
        self.assertEqual(out["date"].dt.strftime("%Y-%m-%d").tolist(),
                         ["2026-11-02", "2026-11-03"])
        self.assertAlmostEqual(float(out.iloc[-1]["close"]), 37.18)

    def test_drop_weekend_rows(self):
        """源站把日期写成 2026/11/1（周日）这种 → 直接剔除。"""
        df = pd.DataFrame({
            "date": pd.to_datetime(["2026-10-30", "2026-11-01", "2026-11-02"]),
            "close": [35.60, 37.42, 27.51],
        })
        out = qs.drop_weekend_rows(df, "500etf")
        self.assertEqual(len(out), 2)
        self.assertNotIn("2026-11-01", out["date"].dt.strftime("%Y-%m-%d").tolist())

    def test_apply_verified_overrides_wild_source_value(self):
        """源站 11-02 算出 27.51（错算），本地核对值 35.62 → 以本地为准。"""
        qs.repair_day("500etf", "2026-11-02", 35.62, reason="双源核对")
        src = pd.DataFrame({
            "date": pd.to_datetime(["2026-10-30", "2026-11-02"]),
            "close": [35.60, 27.51],
        })
        out = qs.apply_verified(src, "500etf")
        self.assertAlmostEqual(float(out.loc[1, "close"]), 35.62)
        self.assertAlmostEqual(float(out.loc[0, "close"]), 35.60)   # 正常日不动

    def test_apply_verified_keeps_source_within_tolerance(self):
        """源站值与实测值只差 2%（常态）→ 保留源站值，不瞎改。"""
        qs._save_cached("500etf", pd.DataFrame({
            "date": pd.to_datetime(["2026-11-03"]),
            "close": [37.18],
            "source": ["intraday@15:00:34"],
            "captured_at": [""],
        }))
        src = pd.DataFrame({"date": pd.to_datetime(["2026-11-03"]), "close": [37.42]})
        out = qs.apply_verified(src, "500etf")
        self.assertAlmostEqual(float(out.loc[0, "close"]), 37.42)

    def test_dedupe_by_date_prefers_verified(self):
        """同一天两行时按出处取舍：verified 压过普通值（不是看谁排在后面）。

        2026-11-09 实况：源站 11-02 给了 27.51（重算垃圾）和 33.81 两行。
        """
        df = pd.DataFrame({
            "date": pd.to_datetime(["2026-11-02", "2026-11-02"]),
            "close": [35.62, 33.81],
            "source": ["verified", "optbbs"],
            "captured_at": ["", ""], "fp": ["", ""], "suspect": ["", ""],
        })
        out = qs.dedupe_by_date(df)
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(float(out.iloc[0]["close"]), 35.62)

    def test_snapshot_keeps_provenance_of_other_rows(self):
        """落当天快照**不能**把历史行的 source 抹成 cached（verified 会失真）。

        2026-11-09 修：老实现 `cur.assign(source="cached")` 把 11-02 的 verified
        标记擦掉，`apply_verified` 的「核对值无条件覆盖」于是永远不触发。
        """
        self._seed_prev(35.60)
        qs.repair_day("500etf", "2026-11-02", 35.62, reason="双源核对")
        with mock.patch.object(qs, "_fetch_minute_table", return_value=list(self.PAIRS)), \
             mock.patch.object(qs, "_is_trading_day", return_value=True):
            r = qs.snapshot("500etf", for_date=date(2026, 11, 5), allow_intraday=True)
        self.assertTrue(r["ok"])
        raw = qs._load_raw("500etf")
        src = dict(zip(raw["date"].dt.strftime("%Y-%m-%d"), raw["source"].astype(str)))
        self.assertEqual(src["2026-11-02"], "verified")       # 出处保住了
        self.assertIn("intraday", src["2026-10-30"])          # 实测行也是实测口径
        self.assertIn("intraday", src["2026-11-05"])

    # ---------------- 2026-11-09：开盘前补采上一交易日 ----------------
    CAL = {date(2026, 10, 28), date(2026, 10, 29), date(2026, 10, 30),
           date(2026, 11, 2), date(2026, 11, 3)}
    PAIRS_FULL = [("9:30:00", 33.10), ("14:56:00", 35.55), ("15:00:00", 35.60)]

    def test_catchup_writes_previous_session_before_open(self):
        """09:25 前，文件里的那份必然是上一交易日的 → 补进 10-30。"""
        qs._save_cached("500etf", pd.DataFrame({
            "date": pd.to_datetime(["2026-10-29"]), "close": [33.31],
            "source": ["intraday@15:00:00"], "captured_at": [""], "fp": ["x"]}))
        with mock.patch.object(qs, "_fetch_minute_table", return_value=list(self.PAIRS_FULL)):
            r = qs.catchup_prev_session("500etf", now=datetime(2026, 11, 2, 8, 35),
                                        calendar=self.CAL)
        self.assertTrue(r["ok"])
        self.assertEqual(r["date"], "2026-10-30")
        self.assertAlmostEqual(float(qs.load_cached("500etf").iloc[-1]["close"]), 35.60)

    def test_catchup_refuses_after_open(self):
        """盘中读到的是当天文件 → 不能往上一交易日写。"""
        with mock.patch.object(qs, "_fetch_minute_table", return_value=list(self.PAIRS_FULL)):
            r = qs.catchup_prev_session("500etf", now=datetime(2026, 11, 2, 10, 0),
                                        calendar=self.CAL)
        self.assertFalse(r["ok"])
        self.assertIn("已开盘", r["reason"])

    def test_catchup_refuses_stale_minute_file(self):
        """文件还是上一交易日那份（指纹一致）→ 拒写，不拿旧值冒充。"""
        fp = qs._fingerprint(self.PAIRS_FULL)
        qs._save_cached("500etf", pd.DataFrame({
            "date": pd.to_datetime(["2026-10-30"]), "close": [35.60],
            "source": ["intraday@15:00:00"], "captured_at": [""], "fp": [fp]}))
        with mock.patch.object(qs, "_fetch_minute_table", return_value=list(self.PAIRS_FULL)):
            # 11-03 早上：要补的是 11-02，而文件还是 10-30 那份
            r = qs.catchup_prev_session("500etf", now=datetime(2026, 11, 3, 8, 35),
                                        calendar=self.CAL)
        self.assertFalse(r["ok"])
        self.assertEqual(r["date"], "2026-11-02")
        self.assertIn("源站未更新", r["reason"])
        self.assertEqual(len(qs.load_cached("500etf")), 1)

    def test_catchup_skips_when_date_already_stored(self):
        qs._save_cached("500etf", pd.DataFrame({
            "date": pd.to_datetime(["2026-10-30"]), "close": [35.60],
            "source": ["intraday@15:00:00"], "captured_at": [""], "fp": [""]}))
        with mock.patch.object(qs, "_fetch_minute_table", return_value=list(self.PAIRS_FULL)):
            r = qs.catchup_prev_session("500etf", now=datetime(2026, 11, 2, 8, 35),
                                        calendar=self.CAL)
        self.assertFalse(r["ok"])
        self.assertIn("已有该日", r["reason"])

    def test_catchup_refuses_incomplete_session(self):
        """末值只到 14:56 之前 → 不是完整收盘序列，拒写。"""
        pairs = [("9:30:00", 33.10), ("14:40:00", 35.00)]
        with mock.patch.object(qs, "_fetch_minute_table", return_value=pairs):
            r = qs.catchup_prev_session("500etf", now=datetime(2026, 11, 2, 8, 35),
                                        calendar=self.CAL)
        self.assertFalse(r["ok"])
        self.assertIn("未到收盘", r["reason"])

    def test_repair_day_is_readable_and_removable(self):
        qs.repair_day("500etf", "2026-11-02", 35.62, reason="t")
        cached = qs.load_cached("500etf")            # 非 suspect → 读得到
        self.assertAlmostEqual(float(cached.iloc[-1]["close"]), 35.62)
        qs.repair_day("500etf", "2026-11-02", None)  # 撤销
        self.assertTrue(qs.load_cached("500etf").empty)

    def test_suspect_row_is_replaced_by_repair(self):
        """11-02 那条假快照（=10-30 收盘）被核对值取代，不留两个 11-02。"""
        qs._save_cached("500etf", pd.DataFrame({
            "date": pd.to_datetime(["2026-11-02"]),
            "close": [35.69],
            "source": ["intraday@15:00:35"],
            "captured_at": [""], "fp": [""], "suspect": ["1"],
        }))
        qs.repair_day("500etf", "2026-11-02", 35.62, reason="t")
        raw = qs._load_raw("500etf")
        self.assertEqual(len(raw), 1)
        self.assertAlmostEqual(float(raw.iloc[0]["close"]), 35.62)
        self.assertEqual(str(raw.iloc[0]["source"]), "verified")



if __name__ == "__main__":
    unittest.main(verbosity=2)
