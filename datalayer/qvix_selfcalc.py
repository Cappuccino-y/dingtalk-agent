"""自算 QVIX（备用数据源）：用上交所官方「期权风险指标」的隐含波动率 + Delta，
按 CBOE 方差互换公式还原 30 天年化隐含波动率。

为什么需要它（2026-10-26）：
    akshare 的 QVIX 全家桶（10 个品种）全部只读 optbbs 的 k.csv。源站 10-23/10-24
    两行没回填，IC 信号只能拒判；而上交所官方接口 **10-23 当天就有数据**（510500
    142 个合约、133 个带 IV）。所以自算一条备用路径。

口径（与 CBOE VIX 一致）：
    1. 同一到期月，用官方 Delta 反解远期价 F：Δ=N(d1) → ln(F/K)=Φ⁻¹(Δ)·σ√T − σ²T/2
       取中位数（只取 0.05≤Δ≤0.95 的合约，避开 0/1 的退化值）。
    2. K0 = 不大于 F 的最大行权价；K<K0 用认沽、K>K0 用认购、K0 取两边均值。
    3. 价格用 Black-76 按官方 IV 复原（不再需要行情价）。
    4. σ² = (2/T)Σ(ΔK/K²)e^{rT}Q(K) − (1/T)(F/K0−1)²，行权价阶梯两端按边界 IV
       外推补全（阶梯太窄会系统性低估方差，2026-10-25 实测约 −11%）。
    5. 两个到期月按 30 天做方差插值，输出年化百分比。

精度定位（2026-10-26 实测，72 个交易日 2026-07-03~10-22）：
    水位 自算/官方 = 1.042±0.058；5 日动量相关 0.83；<-3% 阈值一致 81%；日变化相关 0.75。
    → **够当参考，不够当判据**。所以它只填卡片的「参考值」，判定仍必须用 optbbs 官方值；
      业务侧开关：`IC_QVIX_SELF_REF=0` 可整条关掉。
    （关键坑：上交所官方 Delta 里**认沽是负数**，早先统一按 (0,1) 过滤会把整个认沽侧滤掉，
      左尾只能吃固定 0.3 IV，5 日动量相关会从 0.83 掉到 0.43。）
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parent.parent
CACHE_DIR = Path(REPO / "var" / "qvix")

# 无风险利率（方差互换里 e^{rT} 只影响 1% 量级，取常见的 1.5%）
RISK_FREE = 0.015
DAYS_PER_YEAR = 365.0

# 行权价阶梯两端外推：以边界 IV 补到 ±(这个倍数)
TAIL_EXTEND = 0.45          # 向下最多砍到 K*(1-0.45)，向上最多加到 K*(1+0.45)
TAIL_STEPS = 6              # 每端补几个档

# 只用剩余期限 ≥ 这么多天的到期月（CBOE 同口径），避免临期当月把序列搞得极抖
MIN_DAYS = 24


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """标准正态分位数（Acklam 近似，误差 <1.15e-9，避免为它引入 scipy 依赖）。"""
    if p <= 0.0 or p >= 1.0:
        raise ValueError("p 必须在 (0,1) 内")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
                ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def _black76(F: float, K: float, T: float, sigma: float, r: float, is_call: bool) -> float:
    if T <= 0 or sigma <= 0 or F <= 0 or K <= 0:
        return max(0.0, (F - K) if is_call else (K - F))
    sd = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    df = math.exp(-r * T)
    if is_call:
        return df * (F * _norm_cdf(d1) - K * _norm_cdf(d2))
    return df * (K * _norm_cdf(-d2) - F * _norm_cdf(-d1))


def _fourth_wednesday(year: int, month: int) -> date:
    """上交所 ETF 期权到期日 = 到期月第四个星期三（遇节假日顺延，这里不处理顺延）。"""
    d = date(year, month, 1)
    # 第一个星期三
    offset = (2 - d.weekday()) % 7
    first_wed = d.fromordinal(d.toordinal() + offset)
    return date(first_wed.year, first_wed.month, first_wed.day + 21)


def _parse_chain(df: pd.DataFrame, root: str) -> pd.DataFrame:
    """把官方接口的合约表整理成 [expiry, strike, cp, iv, delta]（剔除调整合约）。"""
    sub = df[df["CONTRACT_ID"].str.startswith(root)].copy()
    rows = []
    for _, r in sub.iterrows():
        cid = str(r["CONTRACT_ID"])
        body = cid[len(root):]
        if len(body) < 8:
            continue
        cp = body[0]
        if cp not in ("C", "P"):
            continue
        rest = body[1:]
        if "M" not in rest:          # 带 A 的是除权调整合约，行权价不可直接解析 → 跳过
            continue
        try:
            yymm, strike_part = rest.split("M", 1)
            if len(yymm) != 4:
                continue
            strike = int(strike_part) / 1000.0
            iv = float(r["IMPLC_VOLATLTY"])
            delta = float(r["DELTA_VALUE"])
        except Exception:  # noqa: BLE001
            continue
        if iv <= 0:
            continue
        # 认购 Delta 在 (0,1)，认沽是**负数**（-1,0）——2026-10-26 修：早先统一要求 (0,1)，
        # 把整个认沽侧全滤掉了，左尾只能用固定 0.3 的 IV 硬顶，直接把精度搞坏。
        if cp == "C" and not (0.0 < delta < 1.0):
            continue
        if cp == "P" and not (-1.0 < delta < 0.0):
            continue
        year, month = 2000 + int(yymm[:2]), int(yymm[2:])
        rows.append({"expiry": _fourth_wednesday(year, month), "strike": strike,
                     "cp": cp, "iv": iv, "delta": delta})
    return pd.DataFrame(rows)


def _forward_from_delta(g: pd.DataFrame, T: float) -> float | None:
    """用官方 Delta 反解远期价：ln(F/K) = Φ⁻¹(Δ)·σ√T − σ²T/2，取中位数。"""
    est = []
    for _, r in g.iterrows():
        # 认购 Delta = N(d1)；认沽 Delta = -N(-d1)（负数），所以 N(d1) = 1 + delta_put
        d = r["delta"] if r["cp"] == "C" else 1.0 + r["delta"]
        if not (0.05 <= d <= 0.95):
            continue
        z = _norm_ppf(d)
        sd = r["iv"] * math.sqrt(T)
        est.append(r["strike"] * math.exp(z * sd - 0.5 * sd * sd))
    if not est:
        return None
    return float(pd.Series(est).median())


def _variance_for_expiry(g: pd.DataFrame, F: float, T: float) -> float | None:
    """单个到期月的年化方差（CBOE 公式）。"""
    if T <= 0 or F <= 0:
        return None
    ivs = g.groupby(["strike", "cp"])["iv"].mean().reset_index()
    piv = ivs.pivot(index="strike", columns="cp", values="iv")
    for side in ("C", "P"):
        if side not in piv.columns:
            piv[side] = float("nan")
    piv = piv[["C", "P"]]
    piv = piv[~piv.isna().all(axis=1)]   # 两边都没有的档位丢掉
    if piv.empty:
        return None
    strikes = sorted(piv.index.tolist())
    k0_candidates = [k for k in strikes if k <= F]
    if not k0_candidates:
        return None
    k0 = max(k0_candidates)

    def _iv_at(k: float, side: str) -> float:
        try:
            v = piv.loc[k, side]
            if pd.notna(v):
                return float(v)
        except KeyError:
            pass
        return float(piv[side].dropna().median()) if piv[side].notna().any() else 0.3

    # 阶梯两端按边界 IV 外推补全（否则深度虚值那段缺失 → 方差系统性低估）
    lo_edge, hi_edge = strikes[0] * (1 - TAIL_EXTEND), strikes[-1] * (1 + TAIL_EXTEND)
    extra_lo = [lo_edge + (strikes[0] - lo_edge) * i / TAIL_STEPS for i in range(TAIL_STEPS)]
    extra_hi = [strikes[-1] + (hi_edge - strikes[-1]) * (i + 1) / TAIL_STEPS for i in range(TAIL_STEPS)]
    iv_low = _iv_at(strikes[0], "P")
    iv_high = _iv_at(strikes[-1], "C")

    rows = []
    for k in sorted(set([*extra_lo, *strikes, *extra_hi])):
        if k <= 0:
            continue
        if k < strikes[0]:
            iv, is_call = iv_low, False
        elif k > strikes[-1]:
            iv, is_call = iv_high, True
        elif abs(k - k0) < 1e-9:
            iv = (_iv_at(k, "C") + _iv_at(k, "P")) / 2.0
            is_call = True
        elif k > k0:
            iv, is_call = _iv_at(k, "C"), True
        else:
            iv, is_call = _iv_at(k, "P"), False
        rows.append((k, _black76(F, k, T, iv, RISK_FREE, is_call)))
    rows.sort()

    total = 0.0
    for i, (k, q) in enumerate(rows):
        if i == 0:
            dk = rows[1][0] - k
        elif i == len(rows) - 1:
            dk = k - rows[i - 1][0]
        else:
            dk = (rows[i + 1][0] - rows[i - 1][0]) / 2.0
        total += dk / (k * k) * math.exp(RISK_FREE * T) * q
    var = 2.0 / T * total - (1.0 / T) * (F / k0 - 1.0) ** 2
    return var if var > 0 else None


def compute_raw(trade_date: str | date, root: str = "510500") -> float | None:
    """算某交易日的 30 天年化隐含波动率（%，**未经校准**）。失败返回 None。"""
    if isinstance(trade_date, (date, datetime)):
        d = trade_date if isinstance(trade_date, date) else trade_date.date()
    else:
        d = datetime.strptime(str(trade_date).replace("-", ""), "%Y%m%d").date()

    import akshare as ak
    try:
        df = ak.option_risk_indicator_sse(date=d.strftime("%Y%m%d"))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"官方期权风险指标拉取失败 {d}: {e}")
        return None
    if df is None or df.empty:
        return None

    chain = _parse_chain(df, root)
    if chain.empty:
        logger.warning(f"{root} {d}: 解析后无有效合约")
        return None

    per_expiry = []
    for expiry, g in chain.groupby("expiry"):
        T = (expiry - d).days / DAYS_PER_YEAR
        if T <= 0:
            continue
        F = _forward_from_delta(g, T)
        if F is None:
            continue
        var = _variance_for_expiry(g, F, T)
        if var is None:
            continue
        per_expiry.append((T, var))
    if len(per_expiry) < 1:
        return None

    per_expiry.sort()
    # 与 CBOE VIX 一致：**只用剩余期限 ≥24 天的到期月**，再按 30 天做方差插值。
    # 用全部到期月（含临期当月）会因临期 gamma 抖动导致序列噪声大增：
    # 2026-10-26 实测 72 个交易日，5 日动量相关 0.43 → 0.69，水位误差 ±11% → ±7%。
    per_expiry = [x for x in per_expiry if x[0] * DAYS_PER_YEAR >= MIN_DAYS] or per_expiry
    target = 30.0 / DAYS_PER_YEAR
    # 找夹住 30 天的两个到期月；只有一个月就直接用它
    below = [x for x in per_expiry if x[0] <= target]
    above = [x for x in per_expiry if x[0] > target]
    if below and above:
        t1, v1 = below[-1]
        t2, v2 = above[0]
        w = (t2 - target) / (t2 - t1)
        var30 = v1 * w + v2 * (1 - w)
    elif below:
        var30 = below[-1][1]
    else:
        var30 = above[0][1]
    return float(math.sqrt(max(var30, 0.0)) * 100.0)


def _cache_path(root: str) -> Path:
    return CACHE_DIR / f"selfcalc_{root}.csv"


def compute_cached(trade_date: str | date, root: str = "510500") -> float | None:
    """带本地缓存的自算（同一天只打一次官方接口）。"""
    d = trade_date if isinstance(trade_date, str) else str(trade_date)
    key = str(d).replace("-", "")
    key = f"{key[:4]}-{key[4:6]}-{key[6:]}" if len(key) == 8 else key
    p = _cache_path(root)
    if p.exists():
        try:
            df = pd.read_csv(p)
            hit = df[df["date"] == key]
            if not hit.empty:
                v = hit.iloc[-1]["close"]
                return float(v) if pd.notna(v) else None
        except Exception:  # noqa: BLE001
            pass
    v = compute_raw(key, root=root)
    if v is not None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df = pd.read_csv(p) if p.exists() else pd.DataFrame(columns=["date", "close"])
        df = pd.concat([df, pd.DataFrame([{"date": key, "close": round(v, 4)}])],
                       ignore_index=True)
        df = df.drop_duplicates(subset=["date"], keep="last").sort_values("date")
        df.to_csv(p, index=False, encoding="utf-8-sig")
    return v
