"""道氏理论 + 艾略特波浪 —— 确定性分析引擎

设计原则（业界最佳实践，参考 Vibe-Trading `skills/elliott-wave`、
LuxAlgo Dow Theory 指标、cluefin `dow.py`）：

    所有点位、目标位、失效位都由本模块**确定性计算**；
    LLM 只负责"叙述"这些结果，**不允许自行计算或凭感觉数浪**。

这解决波浪理论最大的问题——主观性：同一张图两个人能数出两套浪。
本引擎把数浪变成"模板匹配 + 铁律校验 + 斐波那契过滤"，
每条结论都能追到具体的规则通过/失败记录，且**算不出来就明确说算不出来**。

道氏三级别（道氏理论本身要求"三种趋势"分开看）：
    周线 = 主要趋势　日线 = 次级趋势　60分钟 = 短期结构
每个级别都用**百分比 ZigZag** 找摆动点（幅度阈值，与数据长度无关；
固定 K 线窗口在历史数据短时会失衡——日线 180 天用 window=20 只剩 4 个摆动点）。

波浪（艾略特）在日线级别做模板匹配，输出当前浪位 + 目标位 + 失效位 + 置信度。

用法：
    from tools.wave_engine import analyze
    print(analyze("中证1000"))
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Dict, List

FIB_TOL = 0.15  # 斐波那契比率容差（±15%）

# 各周期 ZigZag 阈值（百分数）：primary 定主趋势，secondary 定次级波动
DEGREE_CONFIG = {
    "周线": {"primary": 8.0, "secondary": 4.0, "vol_window": 50},
    "日线": {"primary": 3.0, "secondary": 1.5, "vol_window": 50},
    "60min": {"primary": 1.5, "secondary": 0.6, "vol_window": 30},
}
ELLIOTT_PCT = 3.0        # 波浪分析所用的日线 ZigZag 阈值
ELLIOTT_MIN_BARS = 2     # 每浪最少 K 线数（百分比 ZigZag 下不宜设太大）


# ============================================================ 基础工具
def _pt(i, kind, price, dates) -> dict:
    return {"i": int(i), "kind": kind, "price": float(price), "date": str(dates[i])}


def _zigzag(df, pct: float) -> List[dict]:
    """百分比 ZigZag 摆动点检测（因果，无未来函数）。

    参数 pct 以**百分数**计（3.0 表示 3%）。

    只返回**已确认**的摆动点：当前正在走的那一段（最后一个极值）尚未被
    反向 pct 确认，故不输出——避免"最后一根还没走完就重标"。
    """
    hi = df["high"].astype(float).values
    lo = df["low"].astype(float).values
    dates = df["date"].astype(str).values
    n = len(df)
    if n < 5 or pct <= 0:
        return []
    thr = pct / 100.0

    swings: List[dict] = []
    dir_ = 0                     # 0=未定, 1=寻找高点, -1=寻找低点
    ext_i, ext_p = 0, float(df["close"].iloc[0])
    run_hi_i, run_hi = 0, float(hi[0])
    run_lo_i, run_lo = 0, float(lo[0])

    for i in range(1, n):
        if dir_ == 0:
            if hi[i] >= run_hi:
                run_hi_i, run_hi = i, float(hi[i])
            if lo[i] <= run_lo:
                run_lo_i, run_lo = i, float(lo[i])
            if run_lo <= 0:
                continue
            if (run_hi - run_lo) / run_lo >= thr:
                # 先低后高 → 低点是第一个摆动点并转多；反之高点在前，转向下
                if run_lo_i < run_hi_i:
                    swings.append(_pt(run_lo_i, "L", run_lo, dates))
                    dir_, ext_i, ext_p = 1, run_hi_i, run_hi
                else:
                    swings.append(_pt(run_hi_i, "H", run_hi, dates))
                    dir_, ext_i, ext_p = -1, run_lo_i, run_lo
            continue

        if dir_ == 1:            # 上行段：跟踪最高点
            if hi[i] >= ext_p:
                ext_i, ext_p = i, float(hi[i])
            elif ext_p and (ext_p - lo[i]) / ext_p >= thr:
                swings.append(_pt(ext_i, "H", ext_p, dates))
                dir_, ext_i, ext_p = -1, i, float(lo[i])
        else:                    # 下行段：跟踪最低点
            if lo[i] <= ext_p:
                ext_i, ext_p = i, float(lo[i])
            elif ext_p and (hi[i] - ext_p) / ext_p >= thr:
                swings.append(_pt(ext_i, "L", ext_p, dates))
                dir_, ext_i, ext_p = 1, i, float(hi[i])
    return swings


def _classify_trend(swings: List[dict]) -> tuple:
    """按道氏方法从摆动点序列判断趋势。

    上升：高点抬高(HH) 且 低点抬高(HL)；下降：高点降低(LH) 且 低点降低(LL)；
    否则视为无趋势（震荡）。返回 (趋势, 是否高低点相互印证)。
    """
    highs = [s for s in swings if s["kind"] == "H"]
    lows = [s for s in swings if s["kind"] == "L"]
    if len(highs) < 2 or len(lows) < 2:
        return "数据不足", False

    hh = highs[-1]["price"] > highs[-2]["price"]
    hl = lows[-1]["price"] > lows[-2]["price"]
    if hh and hl:
        return "上升", True
    if (not hh) and (not hl):
        return "下降", True
    return "无趋势（震荡）", False


def _label_swings(swings: List[dict]) -> List[dict]:
    """给每个摆动点打 HH / HL / LH / LL 标签。"""
    last_h = last_l = None
    out = []
    for s in swings:
        rec = dict(s)
        if s["kind"] == "H":
            rec["label"] = "HH" if (last_h is None or s["price"] > last_h) else "LH"
            last_h = s["price"]
        else:
            rec["label"] = "HL" if (last_l is None or s["price"] > last_l) else "LL"
            last_l = s["price"]
        out.append(rec)
    return out


def _in_band(r: float, lo: float, hi: float, tol: float = FIB_TOL) -> bool:
    """比率是否落在 [lo, hi] 区间内（带容差）。"""
    return (lo * (1 - tol)) <= r <= (hi * (1 + tol))


def _fmt_vol(v: float) -> str:
    """成交量按 亿/万 格式化，避免打印原始长数字。"""
    if v >= 1e8:
        return f"{v / 1e8:.1f}亿"
    if v >= 1e4:
        return f"{v / 1e4:.0f}万"
    return f"{v:.0f}"


# ============================================================ 一、道氏理论
def dow_analysis(df, primary_pct: float, secondary_pct: float,
                 vol_window: int = 50) -> Dict:
    """道氏理论分析（确定性）。

    输出：两级摆动点、趋势、相位、量能确认、趋势延续/破坏信号。
    """
    if df is None or len(df) < 20:
        return {"ok": False, "reason": "数据不足"}

    prim = _label_swings(_zigzag(df, primary_pct))
    sec = _zigzag(df, secondary_pct)
    prim_trend, prim_confirmed = _classify_trend(prim)
    sec_trend, _ = _classify_trend(sec)

    # ---- 相位：主级别 × 次级别（LuxAlgo 四象限映射）----
    if prim_trend == "上升" and sec_trend == "上升":
        phase = "主升段（公众参与期）"
    elif prim_trend == "上升" and sec_trend == "下降":
        phase = "派发期（顶部震荡，次级回调）"
    elif prim_trend == "下降" and sec_trend == "上升":
        phase = "积累期（底部震荡，次级反弹）"
    elif prim_trend == "下降" and sec_trend == "下降":
        phase = "下跌段"
    else:
        phase = "方向不明（主／次级信号不一致）"

    # ---- 量能确认：顺趋势的成交量 vs 逆趋势的成交量 ----
    vol_note = "数据不足"
    if "volume" in df.columns and len(df) >= 20:
        tail = df.tail(vol_window)
        closes = tail["close"].astype(float).values
        vols = tail["volume"].astype(float).values
        diff = closes[1:] - closes[:-1]
        up_vol = vols[1:][diff > 0].mean() if (diff > 0).any() else 0.0
        dn_vol = vols[1:][diff < 0].mean() if (diff < 0).any() else 0.0
        if prim_trend == "上升":
            vol_note = ("放量确认（上涨均量 > 下跌均量）" if up_vol > dn_vol
                        else "⚠ 量能背离（上涨均量 < 下跌均量）")
        elif prim_trend == "下降":
            vol_note = ("下跌放量确认" if dn_vol > up_vol
                        else "⚠ 量能背离（下跌缩量）")
        else:
            vol_note = f"震荡（上涨均量 {_fmt_vol(up_vol)} / 下跌均量 {_fmt_vol(dn_vol)}）"

    # ---- 趋势延续 / 破坏：价格与最近已确认摆动点的关系（123 法则）----
    last_close = float(df["close"].astype(float).iloc[-1])
    highs = [s for s in prim if s["kind"] == "H"]
    lows = [s for s in prim if s["kind"] == "L"]
    reversal = "无明确信号"
    if prim_trend == "上升":
        if highs and last_close > highs[-1]["price"]:
            reversal = (f'↑ 已创阶段新高 {last_close:.2f}'
                        f'（突破 {highs[-1]["price"]:.2f}，{highs[-1]["date"]}）')
        elif lows and last_close < lows[-1]["price"]:
            reversal = (f'⚠ 跌破最近摆动低点 {lows[-1]["price"]:.2f}'
                        f'（{lows[-1]["date"]}）—— 上升趋势可能破坏')
        else:
            reversal = "上升趋势延续中（未创新高，也未跌破前低）"
    elif prim_trend == "下降":
        if lows and last_close < lows[-1]["price"]:
            reversal = (f'↓ 已创阶段新低 {last_close:.2f}'
                        f'（跌破 {lows[-1]["price"]:.2f}，{lows[-1]["date"]}）')
        elif highs and last_close > highs[-1]["price"]:
            reversal = (f'⚠ 突破最近摆动高点 {highs[-1]["price"]:.2f}'
                        f'（{highs[-1]["date"]}）—— 下降趋势可能破坏')
        else:
            reversal = "下降趋势延续中（未创新低，也未突破前高）"

    return {
        "ok": True,
        "primary_pct": primary_pct,
        "secondary_pct": secondary_pct,
        "primary_swings": prim,
        "primary_trend": prim_trend,
        "primary_confirmed": prim_confirmed,
        "secondary_trend": sec_trend,
        "phase": phase,
        "volume": vol_note,
        "reversal": reversal,
        "last_close": last_close,
        "last_pivot_age": int(len(df) - 1 - prim[-1]["i"]) if prim else -1,
    }


# ============================================================ 二、艾略特波浪
def _check_structure(seg: List[dict], up: bool) -> Dict:
    """校验波浪结构的三大铁律 + 斐波那契浪间关系。

    seg 长度含义：3=已确认浪1浪2（正走浪3）；4=+浪3（正走浪4）；
    5=+浪4（正走浪5）；6=完整5浪。规则不具备校验条件时记 None（不参与淘汰）。
    """
    x, p1, p2 = seg[0], seg[1], seg[2]
    w1 = abs(p1["price"] - x["price"])
    w2 = abs(p1["price"] - p2["price"])
    checks: List[dict] = []
    fib: List[dict] = []

    # 铁律1：浪2 不能回撤超过浪1 起点
    ok1 = (p2["price"] > x["price"]) if up else (p2["price"] < x["price"])
    checks.append({"rule": "铁律1 浪2未破浪1起点", "pass": ok1})

    r2 = w2 / w1 if w1 else 0.0
    fib.append({"name": "浪2/浪1", "value": r2, "target": "0.5~0.618（上限0.786）",
                "pass": _in_band(r2, 0.5, 0.618) or r2 <= 0.786 * (1 + FIB_TOL)})

    w3 = None
    if len(seg) >= 4:
        p3 = seg[3]
        w3 = abs(p3["price"] - p2["price"])
        # 铁律2：浪3 不是最短（有浪5 时完整判定，否则退化为 ≥浪1）
        if len(seg) >= 6:
            p4, p5 = seg[4], seg[5]
            w5 = abs(p5["price"] - p4["price"])
            ok2 = not (w3 < w1 and w3 < w5)
            note2 = f"浪1={w1:.2f} 浪3={w3:.2f} 浪5={w5:.2f}"
        else:
            ok2 = w3 >= w1
            note2 = f"浪1={w1:.2f} 浪3={w3:.2f}（浪5未走出，暂按 ≥浪1 判定）"
        checks.append({"rule": "铁律2 浪3不是最短", "pass": ok2, "note": note2})
        r3 = w3 / w1 if w1 else 0.0
        fib.append({"name": "浪3/浪1", "value": r3, "target": "1.618（可放宽1.0~2.618）",
                    "pass": _in_band(r3, 1.0, 2.618)})
    else:
        checks.append({"rule": "铁律2 浪3不是最短", "pass": None})

    if len(seg) >= 5:
        p4 = seg[4]
        ok3 = (p4["price"] > p1["price"]) if up else (p4["price"] < p1["price"])
        checks.append({"rule": "铁律3 浪4未进入浪1区域", "pass": ok3})
        r4 = abs(seg[3]["price"] - p4["price"]) / w3 if w3 else 0.0
        fib.append({"name": "浪4/浪3", "value": r4, "target": "0.382（可放宽0.236~0.5）",
                    "pass": _in_band(r4, 0.236, 0.5)})
    else:
        checks.append({"rule": "铁律3 浪4未进入浪1区域", "pass": None})

    return {"checks": checks, "fib": fib}


def _scan_impulse(sw: List[dict], up: bool, min_bars: int) -> List[Dict]:
    """扫描推动浪结构（完整 5 浪 + 进行中的前缀），铁律违反即淘汰。"""
    want = ["L", "H", "L", "H", "L", "H"] if up else ["H", "L", "H", "L", "H", "L"]
    stage_cn = {3: "浪3进行中", 4: "浪4进行中", 5: "浪5进行中", 6: "5浪已完成"}
    hits: List[Dict] = []
    n = len(sw)
    for size in range(6, 2, -1):
        for s in range(0, n - size + 1):
            seg = sw[s:s + size]
            if [p["kind"] for p in seg] != want[:size]:
                continue
            if any(seg[k + 1]["i"] - seg[k]["i"] < min_bars for k in range(size - 1)):
                continue
            res = _check_structure(seg, up)
            hard = [c for c in res["checks"] if c["pass"] is not None]
            if any(not c["pass"] for c in hard):
                continue  # 铁律违反，直接作废
            fib_pass = sum(1 for f in res["fib"] if f["pass"])
            hits.append({
                "up": up, "size": size, "seg": seg, "start_i": seg[0]["i"],
                "stage": stage_cn[size], "rules": res["checks"], "fib": res["fib"],
                "score": len(hard) * 10 + fib_pass,
            })
    return hits


def _targets(hit: Dict, last_close: float) -> Dict:
    """由命中的数浪结构推算下一目标位与失效位（全部确定性计算）。"""
    seg = hit["seg"]
    up = hit["up"]
    size = hit["size"]
    sgn = 1 if up else -1
    x, p1, p2 = seg[0], seg[1], seg[2]
    w1 = abs(p1["price"] - x["price"])
    updown = "突破" if not up else "跌破"

    if size == 3:            # 已确认浪1、浪2 → 正走浪3
        inval = x["price"]
        targets = [p2["price"] + sgn * w1 * k for k in (1.0, 1.618, 2.618)]
        desc = "浪3目标（浪1幅度的 1.0 / 1.618 / 2.618 倍投影）"
        inval_desc = f"{updown}起点 {inval:.2f} 即整个推动浪结构作废"
    elif size == 4:          # 已确认浪3 → 正走浪4
        p3 = seg[3]
        w3 = abs(p3["price"] - p2["price"])
        inval = p1["price"]
        targets = [p3["price"] - sgn * w3 * k for k in (0.236, 0.382, 0.5)]
        desc = "浪4回撤目标（浪3幅度的 0.236 / 0.382 / 0.5 回撤）"
        inval_desc = f"{updown}浪1{'低点' if not up else '高点'} {inval:.2f} 即违反铁律3"
    elif size == 5:          # 已确认浪4 → 正走浪5
        p4 = seg[4]
        inval = p4["price"]
        targets = [p4["price"] + sgn * w1 * k for k in (0.618, 1.0, 1.618)]
        desc = "浪5目标（浪1幅度的 0.618 / 1.0 / 1.618 倍投影）"
        inval_desc = f"{updown}浪4{'高点' if not up else '低点'} {inval:.2f} 即浪5不成立"
    else:                    # 5浪走完 → 进入 ABC 调整
        p4, p5 = seg[4], seg[5]
        w4 = abs(p5["price"] - p4["price"])
        inval = p5["price"]
        targets = [p5["price"] - sgn * w4 * 1.618]
        desc = "5浪已走完，进入 ABC 调整，参考回撤目标"
        inval_desc = f"{updown}浪5{'高点' if not up else '低点'} {inval:.2f} 即确认调整"

    return {"invalidation": inval, "inval_desc": inval_desc,
            "targets": targets, "desc": desc,
            "dist_to_inval": abs(last_close - inval)}


def _freshness(hit: Dict, last_close: float, n_bars: int) -> tuple:
    """判断该数浪结构是否仍能描述**当前**价格位置（还是已经滞后/作废）。

    这是很关键的一步：ZigZag 确认的摆动点本身就是过去式，若价格之后又走了很远，
    一个"浪4进行中"的旧结构可能早已失效，此时把它的目标位当入场依据会害死人。
    返回 (是否仍然有效, 说明)。
    """
    seg, up, size = hit["seg"], hit["up"], hit["size"]

    # ① 时间窗口：最后一个摆动点离现在太远 → 属于历史结构，与当前形态无关
    recent_limit = max(40, int(n_bars * 0.12))
    age = n_bars - 1 - seg[-1]["i"]
    if age > recent_limit:
        return False, (f"该结构的最后一个摆动点在 {age} 根 K 线之前"
                       f"（超过近期窗口 {recent_limit} 根）—— 属于历史结构，"
                       f"不代表当前形态")

    # ② 价格位置是否还符合该结构的预期
    if size == 3:
        x = seg[0]["price"]
        broken = (last_close < x) if up else (last_close > x)
        if broken:
            return False, (f"价格已{'跌破' if up else '突破'}起点 {x:.2f}，"
                           f"该推动浪结构已作废")
        return True, ""
    if size == 4:
        p3 = seg[3]["price"]
        # 浪4 应朝 p1 方向回撤（即离开 p3）；若价格已越过 p3，说明浪3还在延伸
        stale = (last_close > p3) if up else (last_close < p3)
        if stale:
            return False, (f"价格已{'升破' if up else '跌破'}浪3端点 {p3:.2f}，"
                           f"浪3很可能仍在延伸——该数浪**已滞后**，"
                           f"下面的目标位不可直接当入场依据")
        return True, ""
    if size == 5:
        p4 = seg[4]["price"]
        broken = (last_close < p4) if up else (last_close > p4)
        if broken:
            return False, (f"价格已{'跌破' if up else '突破'}浪4端点 {p4:.2f}，"
                           f"浪5不成立")
        return True, ""
    if size >= 6:
        # 5 浪走完后应进入调整；若价格又创出超过浪5 的新极值，说明推动浪延伸了，
        # 「5浪已完成」这个判断作废。
        p5 = seg[5]["price"]
        broken = (last_close > p5) if up else (last_close < p5)
        if broken:
            return False, (f"价格已{'升破' if up else '跌破'}浪5端点 {p5:.2f}，"
                           f"推动浪仍在延伸——「5浪已完成」已不成立")
        return True, ""
    return True, ""


def elliott_analysis(df, swing_pct: float = ELLIOTT_PCT,
                     min_wave_bars: int = ELLIOTT_MIN_BARS,
                     trend: str = "") -> Dict:
    """艾略特波浪分析（确定性）。

    排序原则：**先按时效性过滤，再质量优先** ——
    仍能描述当前价格的结构优先；然后按得分（铁律通过数×10 + 斐波那契通过数）、
    结构完整度、新旧依次排序。这样既不会挑到与主趋势矛盾的三段碎片，
    也不会拿一个早已滞后的旧数浪当结论。

    若连 4 段以上的结构都没有 → valid=False，明确说"无有效推动浪结构"。
    """
    if df is None or len(df) < 20:
        return {"ok": False, "reason": "数据不足"}
    sw = _zigzag(df, swing_pct)
    last_close = float(df["close"].astype(float).iloc[-1])
    if len(sw) < 3:
        return {"ok": False, "reason": f"有效摆动点仅 {len(sw)} 个，不足以构成波浪结构"}

    hits = _scan_impulse(sw, True, min_wave_bars) + _scan_impulse(sw, False, min_wave_bars)
    if not hits:
        return {"ok": False, "swing_count": len(sw),
                "reason": "无任何结构通过三大铁律（数浪不成立，不做波浪推断）"}

    for h in hits:
        h["fresh"], h["fresh_note"] = _freshness(h, last_close, len(df))
        h["aligned"] = bool(trend) and (h["up"] == (trend == "上升"))

    # 时效性 → 得分 → 是否顺势 → 最后摆动点的新旧 → 完整度 → 起点
    hits.sort(key=lambda h: (h["fresh"], h["score"], h["aligned"],
                             h["seg"][-1]["i"], h["size"], h["start_i"]),
              reverse=True)
    best = hits[0]
    tg = _targets(best, last_close)

    size, score = best["size"], best["score"]
    if not best["fresh"]:
        conf, caution = "低（结构已滞后）", best["fresh_note"]
    elif size >= 6 and score >= 30:
        conf, caution = "高", ""
    elif size >= 5:
        conf, caution = "中等", ""
    elif size == 4:
        conf, caution = "中低", "结构尚不完整，仅作位置参考"
    else:
        conf, caution = "低", "仅 3 段已确认，证据不足"

    return {
        "ok": True,
        "swing_pct": swing_pct,
        "swing_count": len(sw),
        "best": best,
        "alts": [h for h in hits[1:5]],
        "last_close": last_close,
        "targets": tg,
        "confidence": conf,
        "caution": caution,
        "valid": size >= 4 and best["fresh"],
        "stale": not best["fresh"],
    }


# ============================================================ 三、渲染
def render_dow(d: Dict, degree_cn: str, period_cn: str) -> str:
    if not d.get("ok"):
        return f"道氏理论 · {degree_cn}（{period_cn}）：{d.get('reason', '分析失败')}"
    L = [f"### 道氏 · {degree_cn}（{period_cn}，ZigZag 阈值 {d['primary_pct']:g}%）"]
    L.append(f"- **主趋势**：{d['primary_trend']}"
             f"（{'高低点相互印证' if d['primary_confirmed'] else '信号不一致，可靠性下降'}）")
    L.append(f"- **次级别趋势**：{d['secondary_trend']}")
    L.append(f"- **相位**：{d['phase']}")
    L.append(f"- **量能确认**：{d['volume']}")
    L.append(f"- **趋势状态**：{d['reversal']}")
    if d.get("last_pivot_age", 0) > 15:
        L.append(f"- ⚠ 最近摆动点在 {d['last_pivot_age']} 根 K 线之前，"
                 f"趋势判定依据较早的结构，仅供参考")
    L.append("- 最近主级摆动点（越靠后越新）：")
    for s in d["primary_swings"][-6:]:
        kind_cn = "高点" if s["kind"] == "H" else "低点"
        L.append(f"    {s['date']}　{kind_cn} {s.get('label', '')}　{s['price']:.2f}")
    return "\n".join(L)


def render_elliott(e: Dict, period_cn: str = "日线", trend: str = "") -> str:
    if not e.get("ok"):
        return f"### 艾略特波浪（{period_cn}）\n- {e.get('reason', '分析失败')}"

    b = e["best"]
    L = [f"### 艾略特波浪（{period_cn}，ZigZag 阈值 {e['swing_pct']:g}%）"]

    # 没有可用的结构（段数不足，或结构已滞后）→ 必须明说，不能拿碎片/旧数浪当结论
    if not e.get("valid"):
        reason = e.get("caution") or "证据不足"
        L.append("- **结论：当前不构成可用的推动浪结构，不做波浪推断**")
        L.append(f"    原因：{reason}")
        L.append(f"    （ZigZag 阈值 {e['swing_pct']:g}%，共 {e['swing_count']} 个摆动点。"
                 f"引擎宁可说『不确定』，也不硬凑一个浪型给你。）")
        L.append("- 已检出的结构（**均不可据此下单**）：")
        for h in [b] + (e.get("alts") or [])[:3]:
            mark = "贴合当前价" if h.get("fresh") else "已滞后"
            interp = ""
            if trend:
                interp = "·顺势" if h.get("aligned") else "·逆势"
            tag = f"{'上升' if h['up'] else '下降'}·{h['stage']}"
            path = " → ".join(f"{p['price']:.2f}" for p in h["seg"])
            L.append(f"    [{tag}·{mark}{interp}] {path}　得分 {h['score']}")
        return "\n".join(L)

    seg = b["seg"]
    tg = e["targets"]
    dir_cn = "上升推动浪（多头结构）" if b["up"] else "下降推动浪（空头结构）"
    L.append(f"- **结构**：{dir_cn}，当前阶段 = **{b['stage']}**")
    L.append(f"- **置信度**：{e['confidence']}"
             + (f"　⚠ {e['caution']}" if e.get("caution") else ""))
    if trend:
        L.append(f"- **与日线主趋势的关系**："
                 f"{'一致（顺势结构）' if b.get('aligned') else '相反（逆势结构，警惕）'}")
    L.append("- **浪序点位**（ZigZag 已确认的摆动点）：")
    for k, p in enumerate(seg):
        tag = ["起点", "浪1", "浪2", "浪3", "浪4", "浪5"][k]
        L.append(f"    {tag}：{p['price']:.2f}（{p['date']}）")
    L.append("- **三大铁律校验**：")
    for c in b["rules"]:
        mark = "通过" if c["pass"] else ("未校验" if c["pass"] is None else "不通过")
        note = f"　{c.get('note', '')}" if c.get("note") else ""
        L.append(f"    [{mark}] {c['rule']}{note}")
    L.append("- **斐波那契校验**：")
    for f in b["fib"]:
        L.append(f"    [{'通过' if f['pass'] else '偏离'}] {f['name']} = {f['value']:.3f}"
                 f"（常见 {f['target']}）")
    L.append(f"- **失效位**：{tg['invalidation']:.2f} —— {tg['inval_desc']}")
    L.append(f"- **目标位**：{tg['desc']}")
    lc = e["last_close"]
    all_reached = True
    for t in tg["targets"]:
        # 失效方向逐尺寸不同：3/4/5 浪是"反向走破"失效；
        # 5 浪已完成则是"顺着再创新极值"才失效，回撤目标在失效位另一侧是正常的
        if b["size"] >= 6:
            beyond = (t > tg["invalidation"]) if b["up"] else (t < tg["invalidation"])
        else:
            beyond = (t < tg["invalidation"]) if b["up"] else (t > tg["invalidation"])
        reached = (lc >= t) if b["up"] else (lc <= t)
        all_reached = all_reached and reached
        tag = ""
        if reached:
            tag += "　✔ 已到达"
        if beyond:
            tag += "　⚠ 已越过失效位（达到即作废）"
        L.append(f"    {t:.2f}{tag}")
    if all_reached:
        L.append("    ⚠ 全部目标位均已到达——该浪可能已走完或已延伸，"
                 "此时追单风险高，宜等新结构")
    if e["alts"]:
        L.append("- **备选数浪**（供交叉验证，不要只信一个）：")
        for a in e["alts"][:3]:
            path = " → ".join(f"{p['price']:.2f}" for p in a["seg"])
            align = ""
            if trend:
                align = "（顺势）" if a.get("aligned") else "（逆势）"
            L.append(f"    {'上升' if a['up'] else '下降'} {a['stage']}{align}：{path}，得分 {a['score']}")
    return "\n".join(L)


# ============================================================ 四、长跨度取数 + 缓存
# 道氏/波浪需要足够长的历史才有意义，项目默认跨度太短
# （日线 180 天→119 根、周线 2 年→104 根）。实测东财：
#   日线/周线**不限制**（日线可回溯 16 年、周线 21 年）；
#   分钟线被限死在约 1~2 个月（60min 最多 ~124 根），故 60min 只能用可用上限。
#
# 为避免「每条消息都全量重拉」导致又慢又被东财封 IP：
#   · 全量长跨度只在**首次**拉一次，之后落磁盘缓存（namespace="wave"）；
#   · 缓存过期时只**增量**抓最近 days_refresh 天，再与缓存按时间拼接；
#   · TTL 内直接命中缓存，零网络请求。
SPAN_CONFIG = {
    "周线":  {"klt": 102, "days_back": 365 * 10, "days_refresh": 90,
              "ttl": 6 * 3600, "cap": 1600},
    "日线":  {"klt": 101, "days_back": 365 * 5, "days_refresh": 20,
              "ttl": 1800, "cap": 1600},
    "60min": {"klt": 60, "days_back": 120, "days_refresh": 10,
              "ttl": 300, "cap": 600},
    "30min": {"klt": 30, "days_back": 120, "days_refresh": 10,
              "ttl": 300, "cap": 800},
}
_CACHE_NS = "wave"
_COLUMN_MAP = {
    "日期": "datetime", "开盘": "open", "最高": "high", "最低": "low",
    "收盘": "close", "成交量": "volume", "成交额": "amount", "换手率": "turnover_rate",
}


def _normalize(df):
    """把 efinance 返回的中文列名规整成引擎内部使用的列名。"""
    import pandas as pd
    df = df.rename(columns=_COLUMN_MAP)
    keep = ["datetime", "open", "high", "low", "close", "volume", "amount", "turnover_rate"]
    df = df[[c for c in keep if c in df.columns]].copy()
    for c in ("open", "high", "low", "close", "volume", "amount", "turnover_rate"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["datetime"] = pd.to_datetime(df["datetime"])
    df["date"] = df["datetime"].dt.strftime("%Y-%m-%d")
    return df.sort_values("datetime").reset_index(drop=True)


def _merge(cached, fresh, cap: int):
    """缓存与新数据按 datetime 拼接；重叠以新数据为准；只保留最后 cap 根。"""
    import pandas as pd
    if cached is None or getattr(cached, "empty", True):
        return fresh
    if fresh is None or fresh.empty:
        return cached
    try:
        cols = [c for c in fresh.columns if c in cached.columns]
        m = pd.concat([cached[cols], fresh[cols]], ignore_index=True)
        m = m.drop_duplicates(subset=["datetime"], keep="last")
        m = m.sort_values("datetime").reset_index(drop=True)
        if len(m) > cap:
            m = m.tail(cap).reset_index(drop=True)
        return m
    except Exception:  # noqa: BLE001
        return fresh


def _kline(index_name: str, klt: int, days_back: int):
    """抓一段 K 线（东财，带 3 次重试）。失败返回 None。"""
    end = datetime.now().strftime("%Y%m%d")
    beg = (datetime.now() - timedelta(days=days_back)).strftime("%Y%m%d")
    for attempt in range(3):
        try:
            import efinance as ef
            df = ef.stock.get_quote_history(stock_codes=index_name, klt=klt,
                                            beg=beg, end=end)
            if df is not None and not df.empty:
                return _normalize(df)
        except Exception:  # noqa: BLE001
            pass
        if attempt < 2:
            time.sleep(0.8 * (attempt + 1))
    return None


def _fetch(index_name: str, period: str):
    """取 K 线：磁盘缓存优先，过期则**增量**抓取后拼接。

    注意：**必须先 import baostock_daily**，它导入时会启用东财反爬补丁
    （NID 令牌 + 随机 UA + 请求延迟 + eastmoney 的 no_proxy 例外）。
    绕过补丁直接调 efinance 会被东财直接断连（RemoteDisconnected）。
    """
    import baostock_daily  # noqa: F401  —— 导入即启用反爬补丁
    from trade_data_fetcher import load_disk_cache, save_disk_cache

    cfg = SPAN_CONFIG.get(period)
    if not cfg:
        from baostock_daily import IndexDataFetcher
        with IndexDataFetcher() as f:
            return f.get_kline_data(index_name, period)

    key = f"{index_name}_{period}"
    # 1) TTL 内直接命中
    fresh = load_disk_cache(key, period, ttl_seconds=cfg["ttl"], namespace=_CACHE_NS)
    if fresh is not None and not fresh.empty:
        return fresh
    # 2) 取"存在但已过期"的旧缓存（极大 TTL 只为把它读出来）
    old = load_disk_cache(key, period, ttl_seconds=1e12, namespace=_CACHE_NS)

    # 3) 有旧缓存 → 只增量抓最近一小段；没有 → 全量抓长跨度
    days = cfg["days_refresh"] if (old is not None and not old.empty) else cfg["days_back"]
    new = _kline(index_name, cfg["klt"], days)

    if new is None or new.empty:
        if old is not None and not old.empty:
            return old                      # 网络失败时用旧数据，总比没有好
        from baostock_daily import IndexDataFetcher   # 最后回退到项目取数器
        with IndexDataFetcher() as f:
            return f.get_kline_data(index_name, period)

    merged = _merge(old, new, cfg["cap"])
    save_disk_cache(key, period, merged, namespace=_CACHE_NS)
    return merged


def analyze(index_name: str = "中证1000", confirm_index: str = "上证指数") -> str:
    """取数 → 道氏（三级别）+ 波浪 → 渲染为可直接交给 LLM 叙述的文本。"""
    parts = [f"# {index_name} 道氏理论 + 艾略特波浪（确定性引擎计算结果）"]
    fetched: Dict[str, object] = {}
    try:
        daily = _fetch(index_name, "日线")
        fetched["日线"] = daily
    except Exception as exc:  # noqa: BLE001
        return f"{index_name} 数据获取失败：{exc}"
    if daily is None or daily.empty:
        return f"{index_name} 日线数据获取失败。"

    parts.append(f"数据截止：{str(daily['date'].iloc[-1])}　最新价："
                 f"{float(daily['close'].iloc[-1]):.2f}")

    # ---------- 一、道氏三级别 ----------
    parts.append("")
    parts.append("## 一、道氏理论 · 三级别")
    degrees = {"周线": "主要趋势", "日线": "次级趋势", "60min": "短期结构"}
    dow_cache: Dict[str, Dict] = {}
    for period, degree_cn in degrees.items():
        cfg = DEGREE_CONFIG[period]
        try:
            if period not in fetched:
                fetched[period] = _fetch(index_name, period)
            df = fetched[period]
            d = dow_analysis(df, cfg["primary"], cfg["secondary"], cfg["vol_window"])
        except Exception as exc:  # noqa: BLE001
            d = {"ok": False, "reason": f"获取失败：{exc}"}
        dow_cache[period] = d
        parts.append(render_dow(d, degree_cn, period))
        parts.append("")

    # 数据跨度（跨度太短时结论参考性会下降，故明示）
    bits = []
    for p, df in fetched.items():
        if df is not None and not getattr(df, "empty", True):
            bits.append(f"{p} {len(df)}根（{df['date'].iloc[0]} ~ {df['date'].iloc[-1]}）")
    parts.append("数据跨度：" + "；".join(bits))
    parts.append("")

    # ---------- 二、指数相互确认（道氏核心）----------
    if confirm_index and confirm_index != index_name:
        parts.append("## 二、指数相互确认（道氏核心）")
        for period in ("周线", "日线"):
            try:
                cdf = _fetch(confirm_index, period)
                cfg = DEGREE_CONFIG[period]
                cd = dow_analysis(cdf, cfg["primary"], cfg["secondary"], cfg["vol_window"])
                t1 = dow_cache.get(period, {}).get("primary_trend")
                t2 = cd.get("primary_trend")
                if t1 == t2 and t1 in ("上升", "下降"):
                    verdict = f"一致（{t1}）→ 趋势获确认"
                elif t1 != t2:
                    verdict = f"背离（{index_name} {t1} vs {confirm_index} {t2}）→ 可靠性下降，需警惕"
                else:
                    verdict = f"两者均为「{t1}」，无明显方向"
                # 依据是否陈旧：最近摆动点离现在太远，趋势判定参考性下降
                ages = [dow_cache.get(period, {}).get("last_pivot_age", 0),
                        cd.get("last_pivot_age", 0)]
                stale = [(period, a) for a in ages if a and a > 15]
                if stale and t1 in ("上升", "下降") and t1 == t2:
                    verdict += (f"（但一方最近摆动点已在 {max(a for _, a in stale)} 根 K 线前，"
                                f"确认依据较陈旧）")
            except Exception as exc:  # noqa: BLE001
                verdict = f"获取 {confirm_index} {period} 失败：{exc}"
                t1 = t2 = "?"
            parts.append(f"- {period}：{index_name} {t1}　|　{confirm_index} {t2}　→　**{verdict}**")
        parts.append("")

    # ---------- 三、艾略特波浪 ----------
    d_trend = dow_cache.get("日线", {}).get("primary_trend", "")
    parts.append("## 三、艾略特波浪（日线）")
    parts.append(render_elliott(elliott_analysis(daily, trend=d_trend), "日线", trend=d_trend))
    parts.append("")
    parts.append("---")
    parts.append("说明：以上点位／趋势／目标／失效位均由引擎确定性计算，"
                 "违反三大铁律的波浪结构已被剔除。"
                 "**请直接叙述这些结果，不要自行重新数浪或臆测点位。**")
    return "\n".join(parts)


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    print(analyze(sys.argv[1] if len(sys.argv) > 1 else "中证1000"))
