"""
IC 抄底信号回测（2026-10-23 新口径）

用**与线上完全同一套代码**（jobs/ic_signal_monitor.py）重算全历史，
输出：新旧口径对比 + 统一持有期口径（入场 = 信号后第一个可交易日开盘）。

口径说明（2026-10-23 定的）：
- **入场**：信号数据日之后的第一个可交易日**开盘价**
  （节前触发被顺延时，入场就是节后首日的开盘）
- **持有**：第 1 个交易日开盘买入 → 第 N 个交易日收盘卖出
- **跨假样本**单独标注 —— 它的"5 日"窗口可能跨 15 个自然日，与其它样本不可比

三条防误判规则（与线上脚本同源）：
1. QVIX 窗口缺日 → 不判定
2. 长假前夜 → 顺延到节后第一个交易日重判
3. 融资强度分级（≤-3% 强 / >-3% 弱），不设硬门槛

用法：
    python jobs/ic_backtest.py              # 全历史
    python jobs/ic_backtest.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for _p in (REPO, HERE, REPO / "datalayer"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import akshare as ak
import numpy as np
import pandas as pd

import ic_signal_monitor as icm   # 线上脚本本体（保证口径一致）
from datalayer.akshare_helper import fetch_qvix

# 旧口径（2026-10-23 之前）：位置位移动量 + 不顺延 + 融资只判 <0
OLD_QVIX_MOMENTUM = "pct_change(位置位移，缺行会错位)"


def load_all() -> pd.DataFrame:
    """中证500 + 500ETF QVIX + 沪市融资余额，合并成一张日线表。"""
    print("拉数据中（中证500 / 500ETF QVIX / 沪市融资余额）...")
    spot = ak.stock_zh_index_daily(symbol="sh000905")
    spot["date"] = pd.to_datetime(spot["date"])
    spot = spot[["date", "open", "close"]].sort_values("date").reset_index(drop=True)

    qvix = fetch_qvix("500etf")
    qvix["date"] = pd.to_datetime(qvix["date"])
    qvix = qvix[["date", "close"]].rename(columns={"close": "qvix"})

    margin = ak.stock_margin_sse(start_date="20150101", end_date="20270101")
    margin = margin.rename(columns={"信用交易日期": "date", "融资余额": "margin"})
    margin["date"] = pd.to_datetime(margin["date"])
    margin = margin[["date", "margin"]].sort_values("date")

    df = spot.merge(qvix, on="date", how="left").merge(margin, on="date", how="left")
    df = df.sort_values("date").reset_index(drop=True)
    print(f"  数据: {df['date'].min().date()} ~ {df['date'].max().date()}（{len(df)} 个交易日）")
    print(f"  融资余额数据到: {df.loc[df['margin'].notna(), 'date'].max().date()}")
    print(f"  QVIX 数据到:    {df.loc[df['qvix'].notna(), 'date'].max().date()}")
    return df


def add_indicators(df: pd.DataFrame, gap_guard: bool) -> pd.DataFrame:
    """加指标列。gap_guard=True → 缺日守门（新口径）；False → 位置位移（旧口径）。"""
    df = df.copy()
    df["rsi14"] = icm.compute_rsi14(df["close"], 14)
    if gap_guard:
        df["qvix_mom_5d"] = icm._mom_with_gap_guard(df["qvix"], 5)
    else:
        df["qvix_mom_5d"] = df["qvix"].pct_change(5, fill_method=None)
    lr = np.log(df["close"] / df["close"].shift(1))
    df["vol_60d"] = lr.rolling(60).std() * np.sqrt(252) * 100
    df["margin_mom20"] = df["margin"].pct_change(20, fill_method=None)
    df["bottom_hit"] = (
        (df["rsi14"] < 44)
        & (df["qvix_mom_5d"] < -0.03)
        & (df["vol_60d"] > 20)
        & (df["margin_mom20"] < 0)
    ).fillna(False)
    return df


def clusters_of(df: pd.DataFrame, mask: pd.Series, gap_days: int = 7) -> list:
    """把触发日聚成"簇"（gap_days 自然日内算同一轮行情）。"""
    idxs = df.index[mask].tolist()
    out, cur = [], None
    for i in idxs:
        if cur is None:
            cur = {"i0": i, "i1": i, "days": [i]}
        elif (df["date"].iloc[i] - df["date"].iloc[cur["i1"]]).days <= gap_days:
            cur["i1"] = i
            cur["days"].append(i)
        else:
            out.append(cur)
            cur = {"i0": i, "i1": i, "days": [i]}
    if cur:
        out.append(cur)
    return out


def forward_stats(df: pd.DataFrame, i0: int) -> dict:
    """统一口径：入场 = 信号日之后第一个可交易日开盘；第 N 个交易日收盘出。"""
    out = {}
    entry_i = i0 + 1
    if entry_i >= len(df):
        return out
    entry = float(df["open"].iloc[entry_i])
    out["entry_date"] = df["date"].iloc[entry_i].strftime("%Y-%m-%d")
    out["entry_price"] = entry
    for h in (5, 10, 20, 60):
        j = i0 + h          # 第 1 个交易日 = i0+1（入场），第 h 个 = i0+h
        if j < len(df):
            out[f"ret{h}"] = (float(df["close"].iloc[j]) / entry - 1) * 100
            out[f"span{h}"] = (df["date"].iloc[j] - df["date"].iloc[i0]).days
        else:
            out[f"ret{h}"] = None
            out[f"span{h}"] = None
    # 持有期内最低/最高（按入场价算）
    for h in (20, 60):
        seg = df.iloc[entry_i: i0 + h + 1]
        if len(seg) >= h:
            out[f"mdd{h}"] = (float(seg["close"].min()) / entry - 1) * 100
            out[f"mfe{h}"] = (float(seg["close"].max()) / entry - 1) * 100
        else:
            out[f"mdd{h}"] = None
            out[f"mfe{h}"] = None
    return out


def describe(df: pd.DataFrame, mask: pd.Series, tag: str, defer_pre_holiday: bool,
             show_detail: bool = True) -> dict:
    """跑一遍某个口径，输出簇与胜率。"""
    work = df.copy()
    if defer_pre_holiday:
        # 规则 2：长假前夜不判定（顺延到节后第一个交易日重判）
        judged = []
        for i in range(len(work)):
            d = work["date"].iloc[i]
            judged.append(not icm.is_pre_long_holiday(d))
        work["judged"] = judged
        work["bottom_hit"] = work["bottom_hit"] & work["judged"]
    else:
        work["judged"] = True

    cls = clusters_of(work, work["bottom_hit"])
    rows = []
    for cl in cls:
        i0 = cl["i0"]
        r = work.iloc[i0]
        st = forward_stats(work, i0)
        level, why = icm.signal_strength({
            "margin_mom20": r["margin_mom20"],
        })
        rows.append({
            "data_date": work["date"].iloc[i0].strftime("%Y-%m-%d"),
            "last_date": work["date"].iloc[cl["i1"]].strftime("%Y-%m-%d"),
            "trigger_days": len(cl["days"]),
            "rsi14": round(float(r["rsi14"]), 1),
            "qvix_mom_5d": round(float(r["qvix_mom_5d"]) * 100, 2),
            "vol_60d": round(float(r["vol_60d"]), 1),
            "margin_mom20": round(float(r["margin_mom20"]) * 100, 2),
            "strength": level,
            "pre_holiday": bool(icm.is_pre_long_holiday(work["date"].iloc[i0])),
            **st,
        })

    def wr(key):
        vals = [x[key] for x in rows if x.get(key) is not None]
        if not vals:
            return f"0/0"
        return f"{sum(1 for v in vals if v > 0)}/{len(vals)}"

    print(f"\n=== {tag} ===")
    print(f"簇数 {len(rows)}")
    if show_detail:
        hdr = (f"  {'数据日':<12}{'触发天':>5}{'RSI':>6}{'QVIX5d%':>9}{'vol60':>7}"
               f"{'融资20d%':>9}{'强度':>5}{'入场日':>12}{'入场价':>9}"
               f"{'5日':>8}{'20日':>8}{'60日':>8}{'跨天':>6}")
        print(hdr)
        for x in rows:
            def fmt(key, unit="%"):
                v = x.get(key)
                return "—" if v is None else f"{v:.1f}{unit}"
            span = x.get("span20")
            print(f"  {x['data_date']:<12}{x['trigger_days']:>5}{x['rsi14']:>6.1f}"
                  f"{x['qvix_mom_5d']:>9.2f}{x['vol_60d']:>7.1f}{x['margin_mom20']:>9.2f}"
                  f"{x['strength']:>5}"
                  f"{x.get('entry_date', '-'):>12}{x.get('entry_price', 0):>9.1f}"
                  f"{fmt('ret5'):>8}{fmt('ret20'):>8}{fmt('ret60'):>8}"
                  f"{('—' if span is None else span):>6}")
    print(f"  胜率（统一口径：次日开盘入场）: 5日 {wr('ret5')} | 20日 {wr('ret20')} | 60日 {wr('ret60')}")
    strong = [x for x in rows if x["strength"] == "强"]
    weak = [x for x in rows if x["strength"] == "弱"]
    if strong:
        v = [x["ret20"] for x in strong if x.get("ret20") is not None]
        print(f"  强信号 {len(strong)} 簇（融资≤-3%）: 20日胜 "
              f"{sum(1 for y in v if y > 0)}/{len(v)}")
    if weak:
        v = [x["ret20"] for x in weak if x.get("ret20") is not None]
        print(f"  弱信号 {len(weak)} 簇（融资>-3%）: 20日胜 "
              f"{sum(1 for y in v if y > 0)}/{len(v)}")
    return {"tag": tag, "clusters": rows}


def main() -> int:
    ap = argparse.ArgumentParser(description="IC 抄底信号回测（新旧口径对比）")
    ap.add_argument("--json", help="把结果写成 JSON")
    args = ap.parse_args()

    df = load_all()
    old = add_indicators(df, gap_guard=False)
    new = add_indicators(df, gap_guard=True)

    res_old = describe(old, old["bottom_hit"], "旧口径（位置位移动量 + 不顺延）",
                       defer_pre_holiday=False)
    res_new = describe(new, new["bottom_hit"],
                       "新口径（QVIX 缺日守门 + 长假前夜顺延 + 融资强弱分级）",
                       defer_pre_holiday=True)

    # 差集：哪些簇被新口径干掉 / 新增
    old_dates = {x["data_date"] for x in res_old["clusters"]}
    new_dates = {x["data_date"] for x in res_new["clusters"]}
    print("\n=== 新旧差异 ===")
    print(f"  被新口径剔除的簇: {sorted(old_dates - new_dates) or '无'}")
    print(f"  新口径新增的簇:   {sorted(new_dates - old_dates) or '无'}")

    if args.json:
        Path(args.json).write_text(
            json.dumps({"old": res_old, "new": res_new,
                        "removed": sorted(old_dates - new_dates),
                        "added": sorted(new_dates - old_dates)},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已写入 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
