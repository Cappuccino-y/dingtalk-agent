"""中证1000 点位图 —— **由 wave_engine 的确定性结果驱动**，不是手画硬编码。

和 analyze() 同一套数据与算法，所以图上的每一个点位都能在 market_wave 的输出里
找到出处：支撑/压力来自道氏摆动点，均线来自收盘均线，浪位/失效位/目标位来自
艾略特波浪引擎的模板匹配结果。

用法（代码）：
    from tools.market_chart import make_index_chart
    path = make_index_chart("中证1000", "chart.png")

用法（命令行）：
    python tools/market_chart.py                     # 生成到临时文件并打印点位摘要
    python tools/market_chart.py --out d:\\chart.png --index 上证指数 --bars 250

注意：图上**不给交易建议**，只画引擎算出来的客观位置。
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for _p in (REPO,):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.wave_engine import (  # noqa: E402
    DEGREE_CONFIG, _fetch, _zigzag, dow_analysis, elliott_analysis,
)

MA_PERIODS = (5, 20, 60)
MA_COLORS = {5: "#8e44ad", 20: "#2980b9", 60: "#16a085"}
RIGHT_PAD = 26          # 右侧留白，用来放点位标签（避免和曲线/其它标签重叠）
NEAR_BAND = (0.92, 1.20)  # 只取现价下方 8% / 上方 20% 内的近端点位


def _merge_levels(items: list, tol: float = 0.004, limit: int = 3) -> list:
    """合并价格过于接近的点位（差 < tol），由高到低取前 limit 个。

    没有这步，图上会出现 5966 / 5971 这种差 5 点的重复标签，纯属噪声。
    """
    out: list = []
    for it in sorted(items, key=lambda d: -d["price"]):
        if all(abs(it["price"] - o["price"]) / max(it["price"], 1.0) > tol
               for o in out):
            out.append(it)
    return out[:limit]


def compute_levels(index_name: str = "中证1000") -> dict:
    """算出画图需要的全部客观点位。"""
    daily = _fetch(index_name, "日线")
    weekly = _fetch(index_name, "周线")
    if daily is None or daily.empty:
        raise RuntimeError(f"{index_name} 日线数据获取失败")

    close = float(daily["close"].astype(float).iloc[-1])
    date_str = str(daily["date"].iloc[-1])

    # ---- 均线 ----
    mas = {}
    for n in MA_PERIODS:
        if len(daily) >= n:
            mas[n] = float(daily["close"].astype(float).rolling(n).mean().iloc[-1])

    # ---- 道氏：三级别趋势 ----
    trends = {}
    for period, cfg in DEGREE_CONFIG.items():
        try:
            df = daily if period == "日线" else _fetch(index_name, period)
            d = dow_analysis(df, cfg["primary"], cfg["secondary"], cfg["vol_window"])
            if d.get("ok"):
                trends[period] = d
        except Exception:  # noqa: BLE001
            trends[period] = {"ok": False}

    # ---- 支撑 / 压力：只取**近端**点位（离现价太远的摆动点没有参考价值）----
    sw = _zigzag(daily, DEGREE_CONFIG["日线"]["primary"])
    lo_band, hi_band = NEAR_BAND
    low_col = daily["low"].astype(float)
    close_col = daily["close"].astype(float)

    lows_b = [s for s in sw if s["kind"] == "L"
              and close * lo_band <= s["price"] < close * 0.999]
    sup = [{"price": s["price"], "label": f"摆动低点 {s['date']}"}
           for s in lows_b[-2:]]
    sup.append({"price": float(low_col.tail(60).min()), "label": "近60日最低"})
    sup.append({"price": float(low_col.tail(20).min()), "label": "近20日最低"})
    supports = _merge_levels(sup)

    highs_a = [s for s in sw if s["kind"] == "H"
               and close * 1.001 <= s["price"] <= close * hi_band]
    res = [{"price": s["price"], "label": f"摆动高点 {s['date']}"}
           for s in highs_a[-2:]]
    res.append({"price": float(close_col.tail(60).max()), "label": "近60日最高"})
    res.append({"price": float(close_col.tail(20).max()), "label": "近20日最高"})
    resists = _merge_levels(res)

    # ---- 波浪：浪位 / 失效位 / 目标位 ----
    d_trend = trends.get("日线", {}).get("primary_trend", "")
    ell = elliott_analysis(daily, trend=d_trend)

    return {
        "index": index_name,
        "date": date_str,
        "close": close,
        "daily": daily,
        "mas": mas,
        "trends": trends,
        "supports": supports,
        "resistances": resists,
        "elliott": ell,
        "d_trend": d_trend,
    }


def render_chart(levels: dict, out_path: str, bars: int = 150) -> str:
    """画点位图并保存，返回文件路径。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
    plt.rcParams["axes.unicode_minus"] = False

    df = levels["daily"].tail(bars).reset_index(drop=True)
    x = list(range(len(df)))
    close = df["close"].astype(float)

    fig, ax = plt.subplots(figsize=(12.5, 6.8), dpi=150)
    ax.plot(x, close, color="#1f4e79", lw=1.9, label=f"{levels['index']} 收盘", zorder=5)

    # ---- 右侧标签统一画在数据区之外，彻底避免重叠 ----
    n = len(df)

    def label(price, text, color, style="--", lw=1.1, z=3):
        ax.axhline(price, color=color, ls=style, lw=lw, zorder=z)
        ax.text(n + 1.2, price, f"{price:,.0f}  {text}", color=color, fontsize=7.6,
                va="center", ha="left")

    for it in levels["supports"]:
        label(it["price"], it["label"], "#1e8449")
    for it in levels["resistances"]:
        label(it["price"], it["label"], "#c0392b")
    for p, v in levels["mas"].items():
        label(v, f"MA{p}", MA_COLORS.get(p, "#666"), style=":", lw=1.3)

    # ---- 波浪：失效位 + 目标位 ----
    ell = levels["elliott"]
    if ell.get("ok") and ell.get("valid"):
        tg = ell["targets"]
        label(tg["invalidation"], "波浪失效位", "#8e44ad", style="-.", lw=1.5, z=4)
        for t in tg["targets"]:
            ax.axhspan(t, t, color="#f39c12", alpha=0)
            ax.axhline(t, color="#e67e22", ls=":", lw=0.9, zorder=2)
            ax.text(n + 1.2, t, f"{t:,.0f}  波浪目标", color="#b9770e",
                    fontsize=7.2, va="center", ha="left")

    # ---- 坐标轴 ----
    ax.set_xlim(-2, n + RIGHT_PAD)
    ticks = list(range(0, n, max(1, n // 8)))
    ax.set_xticks(ticks)
    ax.set_xticklabels([df["date"].iloc[i][5:] for i in ticks], fontsize=8)
    ax.set_ylabel("点位", fontsize=9)
    ax.grid(alpha=0.22, zorder=0)

    # ---- 标题：三级别趋势 + 波浪结论 ----
    def deg(p):
        d = levels["trends"].get(p) or {}
        return d.get("primary_trend", "?") if d.get("ok") else "?"

    head = (f"{levels['index']} 日线（近 {n} 个交易日）· 关键点位\n"
            f"数据截止 {levels['date']}　收盘 {levels['close']:,.2f}　|　"
            f"道氏三级别：周线 {deg('周线')} / 日线 {deg('日线')} / 60分钟 {deg('60min')}")
    if ell.get("ok"):
        if ell.get("valid"):
            head += (f"\n波浪：{'上升' if ell['best']['up'] else '下降'}推动浪 · "
                     f"{ell['best']['stage']} · 置信度 {ell['confidence']}")
        else:
            head += "\n波浪：当前不构成可用的推动浪结构（不做波浪推断）"
    ax.set_title(head, fontsize=10.5)

    handles, labels = ax.get_legend_handles_labels()
    ax.legend(handles, labels, loc="upper left", fontsize=8)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def make_index_chart(index_name: str = "中证1000", out_path: str | None = None,
                     bars: int = 150) -> tuple:
    """一步生成点位图。返回 (图片路径, levels)。"""
    if not out_path:
        from datetime import datetime
        import tempfile
        out_path = os.path.join(
            tempfile.gettempdir(),
            f"{index_name}_chart_{datetime.now():%Y%m%d_%H%M%S}.png")
    levels = compute_levels(index_name)
    return render_chart(levels, out_path, bars=bars), levels


def summary(levels: dict) -> str:
    """纯文本点位摘要（无图时也有用，便于记进日志/消息）。"""
    L = [f"{levels['index']}　{levels['date']}　收盘 {levels['close']:,.2f}"]
    if levels["supports"]:
        L.append("支撑：" + " / ".join(f"{i['price']:,.0f}" for i in levels["supports"]))
    if levels["resistances"]:
        L.append("压力：" + " / ".join(f"{i['price']:,.0f}" for i in levels["resistances"]))
    if levels["mas"]:
        L.append("均线：" + " / ".join(f"MA{n} {v:,.0f}" for n, v in levels["mas"].items()))
    ell = levels["elliott"]
    if ell.get("ok") and ell.get("valid"):
        L.append(f"波浪：失效位 {ell['targets']['invalidation']:,.0f}"
                 f"，目标 " + " / ".join(f"{t:,.0f}" for t in ell["targets"]["targets"]))
    elif ell.get("ok"):
        L.append("波浪：无可用推动浪结构")
    return "\n".join(L)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="生成中证1000 点位图")
    ap.add_argument("--index", default="中证1000")
    ap.add_argument("--out", default=None, help="输出图片路径（默认临时目录）")
    ap.add_argument("--bars", type=int, default=150)
    a = ap.parse_args()
    p, lv = make_index_chart(a.index, a.out, a.bars)
    print("图片:", p, os.path.getsize(p), "bytes")
    print(summary(lv))
