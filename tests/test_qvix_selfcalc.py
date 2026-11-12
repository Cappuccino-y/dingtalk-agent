"""自算 QVIX（datalayer/qvix_selfcalc.py）的离线单元测试。

注意：这个模块目前**没有接入生产线**（2026-10-26 验证精度不达标，见文件头说明），
测试只覆盖确定性部分，不联网。
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _p in (REPO, REPO / "datalayer"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import math

import pandas as pd

import qvix_selfcalc as qs


class TestPieces(unittest.TestCase):
    def test_norm_ppf(self):
        self.assertAlmostEqual(qs._norm_ppf(0.975), 1.959964, places=4)
        self.assertAlmostEqual(qs._norm_ppf(0.5), 0.0, places=6)
        self.assertAlmostEqual(qs._norm_ppf(0.02), -2.053749, places=4)

    def test_fourth_wednesday(self):
        # 2026-10: 10/1 是周四 → 第一个周三是 10/7 → 第四个周三 10/28
        self.assertEqual(qs._fourth_wednesday(2026, 10), date(2026, 10, 28))
        self.assertEqual(qs._fourth_wednesday(2026, 1), date(2026, 1, 28))

    def test_black76_put_call_parity(self):
        F, K, T, sig, r = 6.8, 6.5, 0.08, 0.28, 0.015
        c = qs._black76(F, K, T, sig, r, True)
        p = qs._black76(F, K, T, sig, r, False)
        self.assertAlmostEqual(c - p, math.exp(-r * T) * (F - K), places=8)

    def test_parse_chain_skips_adjusted_contracts(self):
        df = pd.DataFrame({
            "CONTRACT_ID": ["510500C2610M06000", "510500C2612A08750", "510500P2611M06500"],
            "IMPLC_VOLATLTY": [0.25, 0.30, 0.27],
            "DELTA_VALUE": [0.61, 0.35, -0.58],
        })
        out = qs._parse_chain(df, "510500")
        # 带 A 的除权调整合约行权价不可直接解析 → 应被跳过
        self.assertEqual(len(out), 2)
        self.assertEqual(sorted(out["strike"].tolist()), [6.0, 6.5])
        self.assertEqual(sorted(out["cp"].tolist()), ["C", "P"])

    def test_parse_chain_drops_degenerate_iv(self):
        """IV=0 / Delta 顶到 1 的退化合约不能进方差计算。"""
        df = pd.DataFrame({
            "CONTRACT_ID": ["510500C2610M05750", "510500C2610M06000"],
            "IMPLC_VOLATLTY": [0.0, 0.25],
            "DELTA_VALUE": [1.0, 0.61],
        })
        out = qs._parse_chain(df, "510500")
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(float(out.iloc[0]["strike"]), 6.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
