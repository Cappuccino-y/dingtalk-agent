"""用「官方期权风险指标自算 + 锚定链式外推」补 QVIX 缺口。

背景（2026-11-09）：
    optbbs 是 QVIX **唯一**的免费公开源，且它不保证更新。11-09 一整天它的
    分钟文件还是 11-06 那份（指纹逐点相同）、日线 k.csv 最后一行也停在 11-06，
    15:05 快照作业只能拒落库 → IC 信号因为 QVIX 缺一天**整轮拒判**。
    而上交所官方「期权风险指标」是每交易日必发的（只是发得晚，当天傍晚/次日早
    一定有），拿它自算 30 天波动率（`datalayer/qvix_selfcalc.py`）就有序列。

为什么不能直接拿自算值当 QVIX：
    水位差不是常数。实测 12 个交易日（2026-10-16~11-02）自算/官方在
    1.025~1.197 之间漂（均值 1.096、标准差 0.042），直接套会把水位整体抬 10%。
    所以只用**相对变化**，水位锚在最后一个真实 QVIX 上（锚定链式）：
        proxy_t = QVIX_anchor × (self_t / self_anchor)
    实测 12 天配对：日变化相关 0.73、5 日动量相关 0.78、-3% 阈值 7/7 一致。

守门（宁缺勿假，原则和 `qvix_store` 一致）：
    - 锚点必须是**真实值**（源站日线 / 15:05 实测 / 开盘前补采 / 人工核对），
      不能是 suspect，也不能是之前补出来的 proxy（否则误差会一层层滚）；
    - 锚点那天必须有自算值，缺了就整段不补；
    - 相邻两天自算值的比值必须落在 0.5~2.0（波动率一天翻倍/腰斩都不正常，
      说明自算那天数据有问题），越界就停链；
    - 只补最近 `MAX_LOOKBACK` 个交易日内的缺口，太久远的不猜；
    - 写库时 `source=proxy@<锚点日期>`，优先级低于**任何**真实值 —— 源站事后
      回填了，`dedupe_by_date` 会让官方行赢，`proxy` 行不会覆盖任何真实值。
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime

import pandas as pd

try:
    from datalayer import qvix_store as _store
except ImportError:  # 兼容 datalayer 直接进 sys.path 的老路径
    import qvix_store as _store  # type: ignore

logger = logging.getLogger(__name__)

# 品种 → 上交所官方期权链的合约根（selfcalc 用这个根解析 IV/Delta）
# 1000index 是**中金所**股指期权，中金所不发布合约级 IV，暂不支持自算。
SELF_ROOTS = {"500etf": "510500", "50etf": "510050", "300etf": "510300"}

# 最多往回补多少个交易日（再往前就不值得猜了，也避免一次打太多官方接口）
MAX_LOOKBACK = 10

# 相邻两天自算值的比值上限（越过就认为官方那天的数据有问题，停链）
RATIO_MIN, RATIO_MAX = 0.5, 2.0

# proxy 行的出处前缀；`qvix_store._SOURCE_RANK` 里按这个排优先级
PROXY_SOURCE = "proxy"

# 交易日历缓存（一小时内复用）。判据里一次要跑 3 个品种，别每次都打 akshare。
_CAL: set | None = None
_CAL_TS: float = 0.0
_CAL_TTL_S = 3600.0


def _calendar() -> set | None:
    global _CAL, _CAL_TS
    now = time.time()
    if _CAL and now - _CAL_TS < _CAL_TTL_S:
        return _CAL
    days = _store._trade_days()
    if days:
        _CAL, _CAL_TS = days, now
        return days
    return _CAL


def _self_value(qvix_type: str, day: date) -> float | None:
    """某交易日官方自算 QVIX（带本地缓存；失败返回 None）。"""
    root = SELF_ROOTS.get(qvix_type)
    if not root:
        return None
    try:
        from datalayer.qvix_selfcalc import compute_cached
    except ImportError:  # noqa: F401
        from qvix_selfcalc import compute_cached  # type: ignore
    try:
        v = compute_cached(day, root=root)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"自算 QVIX 失败 {qvix_type} {day}: {e}")
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _base_source(v) -> str:
    """出处字段的一级类型（`proxy@2026-11-06` → `proxy`）。"""
    return str(v).strip().lower().split("@")[0]


def _real_mask(raw: pd.DataFrame) -> pd.Series:
    """可作为锚的真实行：非 suspect、非 proxy。"""
    bad = _store._suspect_mask(raw)
    proxy = raw["source"].map(_base_source) == PROXY_SOURCE
    return (~bad) & (~proxy)


def fill_gaps(qvix_type: str, for_date: date | None = None,
              max_lookback: int = MAX_LOOKBACK,
              calendar: set | None = None) -> dict:
    """把最近若干个交易日里缺失的 QVIX 用锚定链式代理补上（幂等）。

    返回 {"ok": bool, "written": [YYYY-MM-DD...], "reason": str}。
    `ok=True` 表示这次至少写下了一天；已补过的日期不会重复补。
    """
    if qvix_type not in SELF_ROOTS:
        return {"ok": False, "written": [],
                "reason": "该品种没有官方 IV 自算源（中金所不发布合约级 IV，暂不支持）"}

    target = for_date or datetime.now().date()
    days = calendar if calendar is not None else _calendar()
    if not days:
        return {"ok": False, "written": [], "reason": "交易日历不可用"}

    # 源站全量快照里已经有的日期同样不用补 —— 那是官方值，proxy 只填空档。
    try:
        src_dates = {d.date() for d in _store.load_full(qvix_type)["date"]}
    except Exception as e:  # noqa: BLE001
        logger.info(f"QVIX {qvix_type}: 读源站全量快照失败（按无该文件处理）: {e}")
        src_dates = set()

    raw = _store._load_raw(qvix_type)
    # 源站后来补上了真值 → 之前那条 proxy 占位该撤（留着只会让人误以为是估算当真相）
    if not raw.empty and src_dates:
        is_proxy = raw["source"].map(_base_source) == PROXY_SOURCE
        filled_later = is_proxy & raw["date"].dt.date.isin(src_dates)
        if filled_later.any():
            gone = raw.loc[filled_later, "date"].dt.strftime("%Y-%m-%d").tolist()
            logger.info(f"QVIX {qvix_type}: 源站已回填，撤掉 proxy 占位 {gone}")
            raw = raw[~filled_later].reset_index(drop=True)
            _store._save_cached(qvix_type, raw)

    # 视为「已有值」的日期：非 suspect（proxy 也算有值，否则每次都会重算）
    if raw.empty:
        covered: set[date] = set()
        anchors: list[tuple[date, float]] = []
    else:
        usable = raw[~_store._suspect_mask(raw)]
        covered = {d.date() for d in usable["date"]}
        real = raw[_real_mask(raw)]
        anchors = [(d.date(), float(c)) for d, c in zip(real["date"], real["close"])]
    covered |= src_dates
    if not anchors:
        return {"ok": False, "written": [],
                "reason": "本地没有可锚定的真实值（源站和快照都还没有任何一天）"}

    phys = [d for d in sorted(days) if d <= target]
    if not phys:
        return {"ok": False, "written": [], "reason": "窗口内没有交易日"}
    tail = phys[-(max(1, int(max_lookback)) + 1):]

    # 把连续缺失的日期切成若干段，每段从「段前最后一个真实值」起链
    runs: list[list[date]] = []
    run: list[date] = []
    for d in tail:
        if d in covered:
            if run:
                runs.append(run)
                run = []
            continue
        run.append(d)
    if run:
        runs.append(run)

    if not runs:
        return {"ok": False, "written": [], "reason": "最近没有缺口"}

    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_rows: list[dict] = []
    skipped: list[str] = []
    for seg in runs:
        anchor = None
        for d, c in anchors:
            if d < seg[0]:
                anchor = (d, c)
            else:
                break
        if anchor is None:
            skipped.append(f"{seg[0]:%Y-%m-%d}~{seg[-1]:%Y-%m-%d}(段前无真实锚点)")
            continue
        a_date, a_close = anchor
        a_self = _self_value(qvix_type, a_date)
        if a_self is None:
            skipped.append(f"{seg[0]:%Y-%m-%d}~{seg[-1]:%Y-%m-%d}"
                           f"(锚点 {a_date:%Y-%m-%d} 无自算值)")
            continue

        prev_close, prev_self = a_close, a_self
        for d in seg:
            s = _self_value(qvix_type, d)
            if s is None:
                skipped.append(f"{d:%Y-%m-%d}(当日无自算值)")
                break                      # 中间断了就别往后推了
            ratio = s / prev_self
            if not (RATIO_MIN <= ratio <= RATIO_MAX):
                logger.warning(f"QVIX {qvix_type} {d}: 自算比值异常 {ratio:.3f}"
                               f"（{prev_self:.2f}→{s:.2f}），停链")
                skipped.append(f"{d:%Y-%m-%d}(自算比值异常 {ratio:.2f})")
                break
            value = prev_close * ratio
            new_rows.append({
                "date": pd.Timestamp(d), "close": round(value, 4),
                "source": f"{PROXY_SOURCE}@{a_date:%Y-%m-%d}",
                "captured_at": now_s, "fp": "", "suspect": "",
            })
            prev_close, prev_self = value, s

    if not new_rows:
        return {"ok": False, "written": [],
                "reason": "；".join(skipped) if skipped else "无可补日期"}

    cur = raw
    if not cur.empty:
        new_dates = {r["date"] for r in new_rows}
        # 只删同一天的非真实行（suspect / 旧 proxy），真实行一律不碰
        is_real = _real_mask(cur)
        cur = cur[~(cur["date"].isin(new_dates) & ~is_real)]
    added = pd.DataFrame(new_rows)
    merged = pd.concat([cur, added], ignore_index=True) if not cur.empty else added
    _store._save_cached(qvix_type, merged)

    written = [r["date"].strftime("%Y-%m-%d") for r in new_rows]
    logger.warning(f"QVIX {qvix_type}: 用自算锚定链式补了 {len(written)} 天"
                   f"（{', '.join(written)}）")
    return {"ok": True, "written": written,
            "reason": "；".join(skipped) if skipped else ""}
