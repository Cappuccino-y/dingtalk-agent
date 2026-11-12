"""「官方自算 + 锚定链式」补 QVIX 缺口（datalayer/qvix_proxy.py）的单元测试。

跑法：
    python -m unittest tests.test_qvix_proxy -v
全部用合成数据 + 桩函数，不写 var/、不打网络。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO, REPO / "datalayer"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np
import pandas as pd

import qvix_store as qs

# qvix_proxy 用 `from datalayer import qvix_store`；把它强制指到同一份模块实例，
# 否则下面改的 qs.CACHE_DIR 对它是另一份（两个模块实例各有一份全局变量，
# 测试就会写到真实的 var/qvix/ 里去）。
sys.modules["datalayer.qvix_store"] = qs

import qvix_proxy as qp  # noqa: E402


def _seed(rows):
    """rows: [(日期, 收盘, source)] 写入本地库。"""
    qs._save_cached("500etf", pd.DataFrame({
        "date": pd.to_datetime([r[0] for r in rows]),
        "close": [r[1] for r in rows],
        "source": [r[2] for r in rows],
        "captured_at": ["" for _ in rows],
        "fp": ["" for _ in rows],
        "suspect": ["" for _ in rows],
    }))


class _TempCacheMixin:
    """把本地库目录指到临时目录，**并且**覆盖 qvix_proxy 实际用的那份模块。

    只改 qs.CACHE_DIR 是不够的：万一 qvix_proxy 绑定的是 datalayer.qvix_store
    那份实例，两者 CACHE_DIR 不同，测试就会写进真实的 var/qvix/（2026-11-09
    全量跑测试时真的发生过，把 11-09 的假值写进了生产库）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._mods = [(m, m.CACHE_DIR) for m in {qs, qp._store}]
        for m, _ in self._mods:
            m.CACHE_DIR = Path(self.tmp.name)
        qp._CAL, qp._CAL_TS = None, 0.0

    def tearDown(self):
        for m, old in self._mods:
            m.CACHE_DIR = old
        self.tmp.cleanup()


class TestProxyFill(_TempCacheMixin, unittest.TestCase):
    CAL = {date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4),
           date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 9)}

    SELF = {
        date(2026, 11, 4): 35.2706,
        date(2026, 11, 5): 33.2163,
        date(2026, 11, 6): 36.7926,
        date(2026, 11, 9): 37.5000,
    }

    def _self(self, qt, d):
        return self.SELF.get(d)

    def test_chains_relative_change_from_real_anchor(self):
        """proxy = 锚点真实值 × (自算当日 / 自算锚点日)，不是直接拿自算值当 QVIX。"""
        _seed([("2026-11-04", 34.07, "cached")])
        with mock.patch.object(qp, "_self_value", side_effect=self._self):
            r = qp.fill_gaps("500etf", for_date=date(2026, 11, 5), calendar=self.CAL)
        self.assertTrue(r["ok"])
        self.assertEqual(r["written"], ["2026-11-05"])
        cached = qs.load_cached("500etf")
        got = float(cached[cached["date"].dt.strftime("%Y-%m-%d") == "2026-11-05"]
                    ["close"].iloc[0])
        self.assertAlmostEqual(got, 34.07 * 33.2163 / 35.2706, places=3)
        self.assertNotAlmostEqual(got, 33.2163, places=2)      # 不能直接用自算水位

    def test_fills_consecutive_missing_days_in_one_chain(self):
        """连续缺 11-05 / 11-06 / 11-09 → 顺着链一路推完。"""
        _seed([("2026-11-04", 34.07, "cached")])
        with mock.patch.object(qp, "_self_value", side_effect=self._self):
            r = qp.fill_gaps("500etf", for_date=date(2026, 11, 9), calendar=self.CAL)
        self.assertEqual(r["written"], ["2026-11-05", "2026-11-06", "2026-11-09"])
        cached = qs.load_cached("500etf")
        self.assertEqual(len(cached), 4)

    def test_is_idempotent(self):
        _seed([("2026-11-04", 34.07, "cached")])
        cal = {date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6)}
        with mock.patch.object(qp, "_self_value", side_effect=self._self):
            r1 = qp.fill_gaps("500etf", for_date=date(2026, 11, 6), calendar=cal)
            r2 = qp.fill_gaps("500etf", for_date=date(2026, 11, 6), calendar=cal)
        self.assertTrue(r1["ok"])
        self.assertFalse(r2["ok"])
        self.assertIn("没有缺口", r2["reason"])
        self.assertEqual(len(qs.load_cached("500etf")), 3)   # 没重复行

    def test_refuses_without_real_anchor(self):
        """本地只有 suspect / proxy 行 → 没有锚点，宁可不补。"""
        _seed([("2026-11-04", 34.07, "proxy@2026-11-03")])
        with mock.patch.object(qp, "_self_value", side_effect=self._self):
            r = qp.fill_gaps("500etf", for_date=date(2026, 11, 5), calendar=self.CAL)
        self.assertFalse(r["ok"])
        self.assertIn("可锚定", r["reason"])

    def test_refuses_when_anchor_self_value_missing(self):
        _seed([("2026-11-04", 34.07, "cached")])
        with mock.patch.object(qp, "_self_value", return_value=None):
            r = qp.fill_gaps("500etf", for_date=date(2026, 11, 5), calendar=self.CAL)
        self.assertFalse(r["ok"])
        self.assertIn("无自算值", r["reason"])

    def test_stops_chain_on_absurd_self_ratio(self):
        """自算值一天翻三倍 → 说明官方数据有问题，停链，不写垃圾值。"""
        _seed([("2026-11-04", 34.07, "cached")])
        vals = dict(self.SELF)
        vals[date(2026, 11, 5)] = 35.2706 * 3.0
        with mock.patch.object(qp, "_self_value", side_effect=lambda qt, d: vals.get(d)):
            r = qp.fill_gaps("500etf", for_date=date(2026, 11, 5), calendar=self.CAL)
        self.assertFalse(r["ok"])
        self.assertIn("比值异常", r["reason"])

    def test_does_not_proxy_dates_the_source_already_has(self):
        """源站全量快照里已经有 11-05 → 那是官方值，不该再用 proxy 占位。"""
        _seed([("2026-11-04", 34.07, "cached")])
        qs.save_full("500etf", pd.DataFrame({
            "date": pd.to_datetime(["2026-11-04", "2026-11-05"]),
            "close": [34.07, 33.5],
        }))
        with mock.patch.object(qp, "_self_value", side_effect=self._self):
            r = qp.fill_gaps("500etf", for_date=date(2026, 11, 5),
                             calendar={date(2026, 11, 4), date(2026, 11, 5)})
        self.assertFalse(r["ok"])
        self.assertIn("没有缺口", r["reason"])

    def test_prunes_proxy_row_after_source_backfills(self):
        """源站事后回填了 11-05 → 之前那条 proxy 占位要被撤掉（避免估算冒充真相）。"""
        _seed([("2026-11-04", 34.07, "cached"),
               ("2026-11-05", 32.09, "proxy@2026-11-04")])
        qs.save_full("500etf", pd.DataFrame({
            "date": pd.to_datetime(["2026-11-04", "2026-11-05"]),
            "close": [34.07, 33.5],
        }))
        qp.fill_gaps("500etf", for_date=date(2026, 11, 5),
                     calendar={date(2026, 11, 4), date(2026, 11, 5)})
        left = qs._load_raw("500etf")
        self.assertEqual(left["date"].dt.strftime("%Y-%m-%d").tolist(), ["2026-11-04"])

    def test_unsupported_type(self):
        r = qp.fill_gaps("1000index", for_date=date(2026, 11, 5), calendar=self.CAL)
        self.assertFalse(r["ok"])
        self.assertIn("没有官方 IV 自算源", r["reason"])

    def test_lookback_limits_how_far_back_it_goes(self):
        """缺口比 lookback 更早 → 不猜。"""
        _seed([("2026-10-20", 30.0, "cached")])
        with mock.patch.object(qp, "_self_value", side_effect=self._self):
            r = qp.fill_gaps("500etf", for_date=date(2026, 11, 5),
                             max_lookback=2, calendar=self.CAL)
        self.assertFalse(r["ok"])


class TestProxyPrecedence(_TempCacheMixin, unittest.TestCase):
    """proxy 只是估算：真实值永远赢，且不允许它去压源站值。"""

    def test_dedupe_prefers_any_real_value_over_proxy(self):
        df = pd.DataFrame({
            "date": pd.to_datetime(["2026-11-05", "2026-11-05"]),
            "close": [32.09, 33.81],
            "source": ["proxy@2026-11-04", "optbbs"],
            "captured_at": ["", ""], "fp": ["", ""], "suspect": ["", ""],
        })
        out = qs.dedupe_by_date(df)
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(float(out.iloc[0]["close"]), 33.81)

    def test_apply_verified_never_overrides_source_with_proxy(self):
        """源站给了 32.5，proxy 估了 30.0（偏离 >8%）→ 仍以源站为准。"""
        qs._save_cached("500etf", pd.DataFrame({
            "date": pd.to_datetime(["2026-11-05"]),
            "close": [30.0], "source": ["proxy@2026-11-04"],
            "captured_at": [""], "fp": [""], "suspect": [""],
        }))
        src = pd.DataFrame({"date": pd.to_datetime(["2026-11-05"]), "close": [32.5]})
        out = qs.apply_verified(src, "500etf")
        self.assertAlmostEqual(float(out.loc[0, "close"]), 32.5)

    def test_proxy_row_does_fill_the_gap_through_overlay(self):
        """源站还没 11-05 这一行 → overlay 用 proxy 把整行补进来（判据才不拒判）。"""
        qs._save_cached("500etf", pd.DataFrame({
            "date": pd.to_datetime(["2026-11-05"]),
            "close": [32.09], "source": ["proxy@2026-11-04"],
            "captured_at": [""], "fp": [""], "suspect": [""],
        }))
        src = pd.DataFrame({"date": pd.to_datetime(["2026-11-04"]), "close": [34.07]})
        out = qs.overlay(src, "500etf")
        self.assertEqual(out["date"].dt.strftime("%Y-%m-%d").tolist(),
                         ["2026-11-04", "2026-11-05"])
        self.assertAlmostEqual(float(out.iloc[-1]["close"]), 32.09)

    def test_proxy_row_is_still_skipped_when_suspect(self):
        """万一某天 proxy 行也被标了 suspect，读的时候照样不能用。"""
        qs._save_cached("500etf", pd.DataFrame({
            "date": pd.to_datetime(["2026-11-05"]),
            "close": [32.09], "source": ["proxy@2026-11-04"],
            "captured_at": [""], "fp": [""], "suspect": ["1"],
        }))
        self.assertTrue(qs.load_cached("500etf").empty)


if __name__ == "__main__":
    unittest.main(verbosity=2)
