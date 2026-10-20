"""
IC 抄底信号周报（各 ETF 历史触发明细图表）
- 三个指数: 上证50(50etf QVIX) / 沪深300(300etf QVIX) / 中证500(500etf QVIX)
- 每个触发日列出: 触发指标 + 后60交易日窗口最高/最低(入场=次日开盘价, 高低点=盘中HIGH/LOW)
- 最高点出现时间 = 相对入场日的自然日数
- 渲染 PNG 供钉钉工作通知发送
"""
import warnings
warnings.filterwarnings("ignore")
import sys
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for _p in (REPO, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import akshare as ak
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

from datalayer.akshare_helper import fetch_qvix

# 周报 PNG 放 var/weekly_reports/（.gitignore 已忽略 var/）
REPORT_DIR = REPO / "var" / "weekly_reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

# 信号参数（与 ic_signal_monitor.py 一致）
RSI_TH, QVIX_TH, VOL_TH = 44, -0.03, 20
WINDOW_TD = 60

INDEXES = [
    ("上证50", "sh000016", "50etf"),
    ("沪深300", "sh000300", "300etf"),
    ("中证500", "sh000905", "500etf"),
]


def _setup_font():
    for f in ["Microsoft YaHei", "SimHei", "DengXian"]:
        if any(f.lower() == x.name.lower() for x in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = f
            break
    plt.rcParams["axes.unicode_minus"] = False


def compute_rsi14(close, period=14):
    d = close.diff()
    g = d.clip(lower=0)
    l = (-d).clip(lower=0)
    ag = g.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    al = l.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    return 100 - 100 / (1 + ag / al.replace(0, float("nan")))


def load_margin_full():
    """分段拉取绕过 stock_margin_sse 2000行静默截断"""
    chunks = [("20100401", "20140630"), ("20140701", "20181231"),
              ("20190101", "20221231"), ("20230101", "20270101")]
    frames = []
    for s, e in chunks:
        m = ak.stock_margin_sse(start_date=s, end_date=e)
        m = m.rename(columns={"信用交易日期": "date", "融资余额": "margin"})
        m["date"] = pd.to_datetime(m["date"])
        frames.append(m[["date", "margin"]])
    return pd.concat(frames).drop_duplicates("date").sort_values("date").reset_index(drop=True)


def load_index(symbol, qvix_type, margin):
    spot = ak.stock_zh_index_daily(symbol=symbol)
    spot["date"] = pd.to_datetime(spot["date"])
    spot = spot.rename(columns={"close": "spot"}).sort_values("date").reset_index(drop=True)
    qvix = fetch_qvix(qvix_type)
    qvix["date"] = pd.to_datetime(qvix["date"])
    df = spot[["date", "spot", "open", "high", "low"]].merge(qvix[["date", "close"]], on="date", how="left")
    df = df.rename(columns={"close": "qvix"}).merge(margin, on="date", how="left")
    df = df.sort_values("date").reset_index(drop=True)
    df["rsi14"] = compute_rsi14(df["spot"])
    df["q5"] = df["qvix"].pct_change(5, fill_method=None)   # 滞后→NaN→不触发
    df["v60"] = np.log(df["spot"] / df["spot"].shift(1)).rolling(60).std() * np.sqrt(252) * 100
    df["m20"] = df["margin"].pct_change(20, fill_method=None)
    df["trig"] = (df["rsi14"] < RSI_TH) & (df["q5"] < QVIX_TH) & (df["v60"] > VOL_TH) & (df["m20"] < 0)
    return df


def collect_rows(df):
    """每个触发日一行: 指标 + 窗口最高/最低 + 最高点+N自然日"""
    n = len(df)
    rows = []
    for fi in df.index[df["trig"]]:
        ni = fi + 1
        r = df.loc[fi]
        row = {"date": r["date"], "spot": r["spot"], "rsi": r["rsi14"],
               "q5": r["q5"], "v60": r["v60"], "m20": r["m20"],
               "mx": np.nan, "mx_lag": np.nan, "mn": np.nan, "open": np.nan}
        if ni < n:
            entry = df.loc[ni, "open"]
            row["open"] = entry
            w = df.loc[ni: min(ni + WINDOW_TD, n - 1)]
            rel_h = w["high"] / entry - 1
            rel_l = w["low"] / entry - 1
            row["mx"] = rel_h.max()
            row["mn"] = rel_l.min()
            row["mx_lag"] = (df.loc[rel_h.idxmax(), "date"] - df.loc[ni, "date"]).days
            row["complete"] = (ni + WINDOW_TD) <= n - 1
        rows.append(row)
    return rows


def _fmt_pct(v, na="--"):
    return f"{v:+.1%}" if pd.notna(v) else na


def render_table(ax, name, rows, y0):
    """在 ax 上渲染一张表格, 返回占用高度"""
    cols = ["触发日", "点位", "RSI14", "QVIX5d", "60d波动", "融资20d",
            "次日开盘", "窗口最高", "最高+N天", "窗口最低"]
    col_x = [0.02, 0.145, 0.215, 0.275, 0.345, 0.415, 0.485, 0.565, 0.66, 0.75]
    lh = 1.0  # 行高
    # 标题
    n_done = sum(1 for r in rows if r.get("complete"))
    ax.text(0.01, y0, f"{name}  （{len(rows)} 次触发，窗口已满 {n_done} 次，入场=次日开盘价，窗口=60交易日）",
            fontsize=11, fontweight="bold", color="#1a1a2e", va="top")
    y0 -= 1.6
    # 表头
    for c, x in zip(cols, col_x):
        ax.text(x, y0, c, fontsize=8.5, fontweight="bold", color="#ffffff", va="center")
    ax.add_patch(plt.Rectangle((0, y0 - 0.55), 1, 0.85, color="#2c3e50", zorder=0))
    y0 -= 1.1
    # 数据行
    for i, r in enumerate(rows):
        complete = r.get("complete", False)
        mx, mn, lag = r["mx"], r["mn"], r["mx_lag"]
        # 颜色语义
        mx_color = "#c0392b" if pd.notna(mx) and mx >= 0.10 else ("#e67e22" if pd.notna(mx) and mx >= 0.05 else "#7f8c8d")
        q5_color = "#27ae60" if pd.notna(r["q5"]) and r["q5"] < -0.20 else "#2c3e50"
        star = "" if complete else " *"
        vals = [
            r["date"].strftime("%Y-%m-%d"),
            f"{r['spot']:.0f}",
            f"{r['rsi']:.1f}",
            _fmt_pct(r["q5"]),
            f"{r['v60']:.0f}%",
            _fmt_pct(r["m20"]),
            f"{r['open']:.0f}" if pd.notna(r["open"]) else "--",
            (_fmt_pct(mx) + star),
            (f"+{lag:.0f}d" if pd.notna(lag) else "--"),
            (_fmt_pct(mn) + star),
        ]
        if i % 2 == 0:
            ax.add_patch(plt.Rectangle((0, y0 - 0.45), 1, 0.9, color="#f5f6fa", zorder=0))
        if not complete:
            ax.add_patch(plt.Rectangle((0, y0 - 0.45), 1, 0.9, color="#fdf6ec", alpha=0.7, zorder=0))
        texts = [
            (r["date"].strftime("%Y-%m-%d"), "#2c3e50"),
            (f"{r['spot']:.0f}", "#2c3e50"),
            (f"{r['rsi']:.1f}", "#2c3e50"),
            (_fmt_pct(r["q5"]), q5_color),
            (f"{r['v60']:.0f}%", "#2c3e50"),
            (_fmt_pct(r["m20"]), "#2c3e50"),
            (f"{r['open']:.0f}" if pd.notna(r["open"]) else "--", "#2c3e50"),
            (_fmt_pct(mx) + star, mx_color),
            (f"+{lag:.0f}d" if pd.notna(lag) else "--", "#8e44ad"),
            (_fmt_pct(mn) + star, "#2980b9"),
        ]
        for (t, c), x in zip(texts, col_x):
            ax.text(x, y0, t, fontsize=8, color=c, va="center")
        y0 -= lh
    return y0


def build_report_png(out_path: Path) -> Path:
    _setup_font()
    margin = load_margin_full()
    all_data = {}
    for name, symbol, qt in INDEXES:
        df = load_index(symbol, qt, margin)
        all_data[name] = collect_rows(df)

    # 布局: 每行0.9单位高度, 三张表+间距
    total_lines = 8 + sum(len(rows) + 6 for rows in all_data.values())
    fig_h = max(6, total_lines * 0.22)
    fig, ax = plt.subplots(figsize=(10.5, fig_h))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, total_lines)
    ax.axis("off")

    now = pd.Timestamp.now()
    y = total_lines - 1
    ax.text(0.01, y, "IC 抄底信号 · 全历史触发明细周报", fontsize=15, fontweight="bold",
            color="#1a1a2e", va="top")
    y -= 1.2
    ax.text(0.01, y, f"生成: {now.strftime('%Y-%m-%d %H:%M')}  |  RSI<44 + QVIX5d<-3% + 60d波动>20% + 融资20日降  |  "
                     f"* = 窗口未走完(当前值)  |  最高点=盘中HIGH相对次日开盘价",
            fontsize=8, color="#7f8c8d", va="top")
    y -= 1.6

    for name, _, _ in INDEXES:
        rows = all_data[name]
        if not rows:
            ax.text(0.01, y, f"{name}: 无触发记录", fontsize=10, va="top")
            y -= 3
            continue
        y = render_table(ax, name, rows, y) - 2.5

    # 汇总统计
    y -= 0.5
    ax.text(0.01, y, "汇总（窗口已满样本）", fontsize=11, fontweight="bold", va="top")
    y -= 1.2
    for name, _, _ in INDEXES:
        done = [r for r in all_data[name] if r.get("complete")]
        if not done:
            continue
        mx = pd.Series([r["mx"] for r in done])
        lag = pd.Series([r["mx_lag"] for r in done])
        ax.text(0.03, y, f"{name}: n={len(done)}  最高>0: {(mx>0).mean():.0%}  ≥+5%: {(mx>=0.05).mean():.0%}  "
                         f"均值 {mx.mean():+.1%}  中位 {mx.median():+.1%}  |  最高点>60自然日: {(lag>60).mean():.0%}",
                fontsize=8.5, va="top", color="#2c3e50")
        y -= 1.0

    fig.savefig(out_path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out_path


def generate_and_send(force: bool = False):
    """生成周报PNG并经钉钉工作通知发送。周一调用; 当日已生成则跳过(幂等)。"""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"ic_weekly_{pd.Timestamp.now().strftime('%Y%m%d')}.png"
    if out.exists() and not force:
        return out
    build_report_png(out)
    try:
        from tools.notifycation import send_dingtalk_image
        ok = send_dingtalk_image(str(out), title="IC抄底信号周报")
        print(f"WEEKLY_REPORT:{out} sent={ok}")
    except Exception as e:
        print(f"WEEKLY_REPORT_SEND_FAIL:{e}")
    return out


if __name__ == "__main__":
    force = "--force" in sys.argv
    p = generate_and_send(force=force)
    print(f"OK {p}")
