"""QVIX 本地日线自建库（补 optbbs 日线回填滞后的缺口）。

背景（2026-10-26 排查）：
    optbbs（`http://1.optbbs.com/d/csv/d/k.csv`）是 QVIX 唯一的免费源。
    它的日线**行是按日历预建的**，值由站点事后回填。2026-10-23（周五）和
    10-24 两行至今为空，且 50ETF / 300ETF / 500ETF 三个品种**同时**缺 ——
    属源站侧停更，不是本地抓取或代码问题（2022-09 以来 984 个交易日里
    只有这两天是空的，说明平时回填是正常的）。

对策：**自建尾部**，不再把「源站什么时候回填」当成前提。
    每个交易日 15:05（收盘后）从 optbbs 的分钟文件（`vix500.csv` 等，只保留
    当天 240 根）取最后一个有效 QVIX，落到 `var/qvix/qvix_<type>_daily.csv`。
    读取时用本地库**只补空缺、不覆盖** optbbs 的日线值，所以源站后来回填了
    也以源站为准。

为什么不用「上一交易日值顺延」：顺延会让 5 日动量整段错位（等于把陈旧值当成
新值参与计算），2026-09-11 / 10-12 的假触发就是这么来的。本地库落的是**当天
真实收盘 QVIX**，不存在这个问题。

2026-11-02 追加：源站的日线文件（`k.csv`，约 900KB）偶尔会**下到一半被截断**
（`http.client.IncompleteRead`），一次就把整个 IC 作业打成 exit 1。除了在
`akshare_helper.fetch_qvix` 里加重试/超时，这里再存一份**整段日线的全量快照**
（`qvix_<type>_daily_full.csv`），源站彻底不可用时用它照常算指标。

2026-11-03 追加：分钟文件也会「不更新」。11-02（周一）15:05 抓到的 vix500/vix50/
vix300 与 10-30（周五）**逐点一致**（末值都是 35.69 / 16.03 / 20.13），结果是
把周五的收盘当成周一的落库了 —— 而 11-02 真实收盘是 35.62。这类假值一旦被
`overlay` 拿去补日线缺口，等于拿旧值冒充新值（正是本模块开头反对的那种做法）。
对策：落库前用**分钟序列指纹**（有效根数 + 首值 + 末值 + 中位数）和上一交易日比，
完全一致 → 判「源站未更新」拒落库（作业失败会按窗口自动重试，源站后来更新就能补上）；
上一交易日是旧版本写的、没有指纹时退化成「末值 == 昨收」，只标 `suspect` 仍落库，
`overlay` 默认跳过 suspect 行。

2026-11-04 追加：源站**重算出来的日线行也会错**。11-04 早上 k.csv 才把 11-02 那行
补出来，值却是 27.51（当天分钟文件「昨收」= 35.62、上交所期权链自算 36.52，两路都不
支持 27.51），同一批还多出一行日期写成 `2026/11/1`（周日）的 37.42。也就是说源站
事后补行时可能拿残缺的期权链重算，数值差到 20%+。对策两条：
  1. `drop_weekend_rows()`：源站出现的周六/周日行直接丢掉（`2026/11/1` 就是这种）；
  2. `apply_verified()`：本地有**实测/已核对**的值（15:05 快照或 `repair_day` 写入的
     verified 行），而源站同一天的值偏离超过阈值（默认 8%）→ 以本地为准。
     不这么做的后果：缺口是补上了，但补进来的是垃圾值，3 日动量能算出 +35%。

2026-11-09 追加（第二种数据源）：源站会**整天不更新**（11-09 分钟文件仍是 11-06 那份、
日线也停在 11-06），15:05 快照只能拒落库，IC 判据因为缺一天整轮拒判。对策是用
**上交所官方「期权风险指标」自算 + 锚定链式外推**补缺口（`datalayer/qvix_proxy.py`），
写进来的行 `source=proxy@<锚点日期>`：
  1. 排名低于 verified/intraday/catchup/普通源站值（`_SOURCE_RANK` 里是 3，只压 suspect）；
  2. `apply_verified` **不允许** proxy 覆盖源站值（它是估算，不是实测）；
  3. 所以源站事后回填时 proxy 行会自然让位，不需要人工清理。
"""
from __future__ import annotations

import csv
import io
import logging
import math
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parent.parent
CACHE_DIR = Path(os.environ.get("QVIX_CACHE_DIR") or (REPO / "var" / "qvix"))

# 分钟文件的品种代码（optbbs 命名，和 akshare 的 qvix_type 对应）
MINUTE_CODES = {
    "50etf": "vix50",
    "300etf": "vix300",
    "500etf": "vix500",
    "1000index": "vixindex1000",
}
MINUTE_URL = "http://1.optbbs.com/d/csv/d/{code}.csv"

COLUMNS = ["date", "close", "source", "captured_at", "fp", "suspect"]

# 出处可信度（数字越小越可信）：verified 是人工/双源核对过的值，
# intraday 是收盘后实测快照，其余（含旧版本写的 cached）只能算普通值。
# proxy 是「官方 IV 自算 + 锚定链式」补出来的估算值（datalayer/qvix_proxy.py），
# **任何真实值都比它可信** —— 源站事后回填了就该让它赢。
_SOURCE_RANK = {"verified": 0, "intraday": 1, "catchup": 1, "proxy": 3}
_RANK_OTHER = 2
_RANK_SUSPECT = 10      # 疑似陈旧值排最后，只在没有别的行时才用


def _cache_path(qvix_type: str) -> Path:
    return CACHE_DIR / f"qvix_{qvix_type}_daily.csv"


def _load_raw(qvix_type: str) -> pd.DataFrame:
    """读本地库原始表（带 source / fp / suspect 等附加列），缺失列补齐。

    和 `load_cached` 的区别：那个只回 date/close（给指标计算用），这个要拿到
    指纹和 suspect 标记（给落库守门和 overlay 过滤用）。
    """
    p = _cache_path(qvix_type)
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS)
    try:
        df = pd.read_csv(p)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"QVIX 本地库读取失败 {p}: {e}")
        return pd.DataFrame(columns=COLUMNS)
    if df.empty or "date" not in df.columns or "close" not in df.columns:
        return pd.DataFrame(columns=COLUMNS)
    for c in COLUMNS:
        if c not in df.columns:
            df[c] = ""
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["date", "close"])
    df = df[df["close"] > 0]
    # 同一日期多行时按出处可信度取（verified > intraday > 其它 > suspect）
    return dedupe_by_date(df[COLUMNS])


def load_cached(qvix_type: str, include_suspect: bool = False) -> pd.DataFrame:
    """读本地自建日线。返回列 [date, close]（date 为 datetime）。空则返回空表。

    include_suspect=False（默认）会剔掉 suspect 行 —— 那些是「源站可能没更新」
    的可疑值，宁可当缺，也不拿来冒充新值。
    """
    df = _load_raw(qvix_type)
    if df.empty:
        return pd.DataFrame(columns=["date", "close"])
    if not include_suspect:
        bad = _suspect_mask(df)
        if bad.any():
            df = df[~bad]
    return df[["date", "close"]].reset_index(drop=True)


def _suspect_mask(df: pd.DataFrame) -> pd.Series:
    """suspect 标记（1/""）→ 布尔掩码；列不存在时全 False。"""
    if df is None or df.empty or "suspect" not in df.columns:
        return pd.Series(False, index=df.index if df is not None else None)
    return df["suspect"].astype(str).str.strip().isin(("1", "1.0", "True", "true"))


def _source_rank(df: pd.DataFrame) -> pd.Series:
    """每行的出处可信度（verified 0 / intraday 1 / 其它 2 / suspect +10）。"""
    src = df["source"].astype(str).str.strip().str.lower()
    base = src.str.split("@").str[0]
    rank = base.map(lambda s: _SOURCE_RANK.get(s, _RANK_OTHER)).astype("int16")
    return rank + _suspect_mask(df).astype("int16") * _RANK_SUSPECT


def dedupe_by_date(df: pd.DataFrame) -> pd.DataFrame:
    """同一 date 只留一行，按**出处可信度**取舍（不是简单留最后一行）。

    2026-11-09 修：optbbs 的 k.csv 会给同一天吐出两行 —— 11-02 就同时有
    `27.51`（源站事后重算的垃圾值）和 `33.81`。以前只有下游 `keep="last"`，
    等于看运气挑一行；挑错就把 5 日动量的基准日整体带歪（窗口按位置位移）。
    这里改成：verified > intraday > 其它 > suspect，同档保留文件里靠后的那条。
    """
    if df is None or df.empty or "date" not in df.columns:
        return df
    out = df.copy()
    for c in COLUMNS:
        if c not in out.columns:
            out[c] = ""
    out["_rank"] = _source_rank(out)
    out = (out.sort_values(["date", "_rank"], kind="stable")
              .drop_duplicates(subset=["date"], keep="first")
              .drop(columns=["_rank"])
              .sort_values("date").reset_index(drop=True))
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    return out


def _save_cached(qvix_type: str, df: pd.DataFrame) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    for c in COLUMNS:
        if c not in out.columns:
            out[c] = ""
    out = out[COLUMNS]
    out = dedupe_by_date(out)
    out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
    # utf-8-sig：便于 Excel 直接打开核对
    out.to_csv(_cache_path(qvix_type), index=False, encoding="utf-8-sig")


def _full_path(qvix_type: str) -> Path:
    return CACHE_DIR / f"qvix_{qvix_type}_daily_full.csv"


def save_full(qvix_type: str, df: pd.DataFrame) -> int:
    """把源站整段日线落一份本地全量快照，返回总行数。

    和 `_save_cached`（只落每天 15:05 那一条收盘值）是两回事：这里存的是
    **源站整段历史**，用于「源站整体挂掉 / 反复截断」时仍能算出 RSI、动量这些
    指标（缺的只是最后一天，守门会判拒判，不会拿旧值冒充新值）。

    写入时和旧快照合并（keep=last），源站这次返回更短也不会把历史吃掉。
    """
    if df is None or df.empty or "date" not in df.columns or "close" not in df.columns:
        return 0
    out = pd.DataFrame({
        "date": pd.to_datetime(df["date"], errors="coerce"),
        "close": pd.to_numeric(df["close"], errors="coerce"),
    })
    out = out.dropna(subset=["date", "close"])
    out = out[out["close"] > 0]
    if out.empty:
        return 0
    old = load_full(qvix_type)
    if not old.empty:
        out = pd.concat([old, out], ignore_index=True)
    out = (out.drop_duplicates(subset=["date"], keep="last")
              .sort_values("date").reset_index(drop=True))
    p = _full_path(qvix_type)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.copy()
    tmp["date"] = tmp["date"].dt.strftime("%Y-%m-%d")
    tmp.to_csv(p, index=False, encoding="utf-8-sig")
    return len(out)


def load_full(qvix_type: str) -> pd.DataFrame:
    """读本地全量快照。返回列 [date, close]（date 为 datetime）；没有则空表。"""
    p = _full_path(qvix_type)
    if not p.exists():
        return pd.DataFrame(columns=["date", "close"])
    try:
        df = pd.read_csv(p)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"QVIX 全量快照读取失败 {p}: {e}")
        return pd.DataFrame(columns=["date", "close"])
    if df.empty or "date" not in df.columns or "close" not in df.columns:
        return pd.DataFrame(columns=["date", "close"])
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["date", "close"])
    df = df[df["close"] > 0]
    return df[["date", "close"]].sort_values("date").reset_index(drop=True)


def _is_trading_day(d: date) -> bool | None:
    """A 股交易日判断。查不到日历（网络/接口故障）时返回 None（调用方自行降级）。"""
    try:
        import akshare as ak
        cal = ak.tool_trade_date_hist_sina()
        days = {pd.Timestamp(x).date() for x in cal["trade_date"]}
        return d in days
    except Exception as e:  # noqa: BLE001
        logger.warning(f"交易日历获取失败（跳过校验）: {e}")
        return None


def _trade_days() -> set[date] | None:
    """整份交易日历（一次性拉，别按天反复拉）。失败返回 None。"""
    try:
        import akshare as ak
        cal = ak.tool_trade_date_hist_sina()
        return {pd.Timestamp(x).date() for x in cal["trade_date"]}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"交易日历获取失败: {e}")
        return None


def last_trading_day_before(day: date, calendar: set[date] | None = None) -> date | None:
    """day 之前最近的一个交易日（含跨周末/长假）。日历不可用返回 None。"""
    days = calendar if calendar is not None else _trade_days()
    if not days:
        return None
    past = [d for d in days if d < day]
    return max(past) if past else None


def _fetch_minute_table(qvix_type: str, timeout: float = 15.0) -> list[tuple[str, float]]:
    """抓当天分钟 QVIX，返回 [(time, value)]（只保留有值的行）。"""
    import requests

    code = MINUTE_CODES.get(qvix_type)
    if not code:
        raise ValueError(f"未知 qvix_type: {qvix_type}")
    resp = requests.get(MINUTE_URL.format(code=code), timeout=timeout)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    rows = list(csv.reader(io.StringIO(resp.text)))
    if not rows:
        return []
    out: list[tuple[str, float]] = []
    for row in rows[1:]:
        if len(row) < 2:
            continue
        t, v = row[0].strip(), row[1].strip()
        if not v:
            continue
        try:
            out.append((t, float(v)))
        except ValueError:
            continue
    return out


def _fingerprint(pairs: list[tuple[str, float]]) -> str:
    """分钟序列指纹：有效根数 + 首个有效值 + 末值 + 中位数（都保留 2 位）。

    用来识别「源站当天没更新，把上一交易日的分钟文件原样再发一遍」。这种情况
    整条路径逐点相同，指纹必然一致；而真实的不同交易日即使收盘值**巧合**相等
    （988 个交易日里出现过 19 次），盘中路径也不会完全重合，所以指纹能区分开
    —— 只比收盘值会把 2% 的正常日子误杀。
    """
    vals = [float(v) for _, v in pairs if v is not None]
    if not vals:
        return ""
    ordered = sorted(vals)
    med = ordered[len(ordered) // 2]
    return f"{len(vals)}|{vals[0]:.2f}|{vals[-1]:.2f}|{med:.2f}"


def _fp_str(v) -> str:
    """指纹字段规范化：CSV 里空值读回来是 NaN，别让它变成字符串 "nan" 冒充指纹。"""
    s = str(v).strip()
    return "" if s.lower() in ("", "nan", "none", "<na>") else s


def _prev_row(qvix_type: str, target: date) -> tuple[pd.Series | None, bool]:
    """上一交易日的本地记录 + 该记录是否可信（有指纹 = 新版本写的）。

    优先取「有指纹」的最后一行当基准；都没有（老版本写的）时退回最后一行，
    调用方只用它做「末值 == 昨收」的弱判据。
    """
    raw = _load_raw(qvix_type)
    if raw.empty:
        return None, False
    prev = raw[raw["date"] < pd.Timestamp(target)]
    if prev.empty:
        return None, False
    # suspect 行本身就是可疑值，不能当基准（11-02 那条假 35.69 就是）
    prev = prev[~_suspect_mask(prev)]
    if prev.empty:
        return None, False
    trusted = prev[prev["fp"].map(_fp_str) != ""]
    if not trusted.empty:
        return trusted.iloc[-1], True
    return prev.iloc[-1], False


def _stale_snapshot_reason(prev: pd.Series, fp: str) -> str | None:
    """当天分钟序列与上一交易日**逐点相同** → 源站没更新，不能落库。"""
    prev_fp = _fp_str(prev.get("fp"))
    if not (prev_fp and fp and prev_fp == fp):
        return None
    return (f"疑似源站未更新：当天分钟序列与 {prev['date']:%Y-%m-%d} 完全一致"
            f"（指纹 {fp}），不落库")


def _minute_time(item: tuple[str, float]) -> time:
    """分钟行的时间（9:30:00 / 15:00:00）→ time。解析不了按 00:00 处理。"""
    try:
        return datetime.strptime(str(item[0]), "%H:%M:%S").time()
    except (TypeError, ValueError):
        return datetime.min.time()


def snapshot(qvix_type: str, for_date: date | None = None,
             allow_intraday: bool = False) -> dict:
    """把当天收盘 QVIX 落到本地库。

    返回 {"ok": bool, "date": str, "close": float, "rows": int, "reason": str}。
    规则：
      - 未收盘（<15:00）默认拒绝，避免把盘中值当成收盘值落库；
      - 非交易日拒绝；
      - 当天没有任何有效分钟值 → 不落库（源站当天也没算出来，等地推）；
      - 分钟序列与上一交易日完全相同 → 判源站未更新，不落库（2026-11-02 的教训）。
    """
    now = datetime.now()
    target = for_date or now.date()

    if not allow_intraday and now.time() < datetime.strptime("15:00", "%H:%M").time():
        return {"ok": False, "date": str(target), "reason": "未收盘，不落盘"}

    trading = _is_trading_day(target)
    if trading is False:
        return {"ok": False, "date": str(target), "reason": "非交易日"}

    try:
        pairs = _fetch_minute_table(qvix_type)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "date": str(target), "reason": f"抓取失败: {e}"}

    if not pairs:
        return {"ok": False, "date": str(target),
                "reason": "分钟文件当天无有效值（源站未计算）"}

    # 分钟文件的时间形如 9:30:00 / 15:00:00，字符串排序不可靠 → 按解析后的时间取最大
    t_last, v_last = max(pairs, key=_minute_time)
    fp = _fingerprint(pairs)

    # 守门：源站有时「没更新但不报错」——整份分钟文件还是上一交易日的。
    prev, prev_trusted = _prev_row(qvix_type, target)
    suspect = ""
    if prev is not None:
        reason = _stale_snapshot_reason(prev, fp)
        if reason:
            logger.warning(f"QVIX {qvix_type} {target}: {reason}")
            return {"ok": False, "date": str(target), "reason": reason}
        if not prev_trusted:
            # 上一交易日是旧版本写的（没指纹）→ 只能弱判：末值与昨收完全一样。
            # 这种情况历史上 988 天里出现过 19 次真巧合，所以**不拒**，只标 suspect
            # （overlay 默认跳过），等源站日线回填后自然修正。
            try:
                same = abs(float(v_last) - float(prev["close"])) < 0.005
            except (TypeError, ValueError):
                same = False
            if same:
                suspect = "1"
                logger.warning(
                    f"QVIX {qvix_type} {target}: 末值 {v_last} 与上一交易日 "
                    f"{prev['date']:%Y-%m-%d} 相同且该日无指纹，标记 suspect（仍落库）")

    row = {"date": str(target), "close": round(float(v_last), 4),
           "source": f"intraday@{t_last}", "captured_at": now.strftime("%Y-%m-%d %H:%M:%S"),
           "fp": fp, "suspect": suspect}

    cur = _load_raw(qvix_type)
    ts = pd.Timestamp(target)
    if not cur.empty:
        # 2026-11-09 修：老写法 `cur.assign(source="cached", captured_at="")` 会把
        # **所有历史行**的出处抹成 cached —— repair_day 写的 verified 标记、之前
        # 15:05 实测的 intraday 标记全没了，`apply_verified` 的「核对值无条件覆盖」
        # 于是永远不触发（11-02 的 33.81 就是这么混进全量快照的）。
        # 现在只动目标日期这一行：已有 verified 就原样留着（verified 压过实测快照），
        # 其它同一天的行先删掉，再写这次的实测值。
        same_day = cur[cur["date"] == ts]
        verified_here = same_day[
            same_day["source"].astype(str).str.lower().str.startswith("verified")]
        if verified_here.empty:
            cur = cur[cur["date"] != ts]
    new = pd.DataFrame([{**row, "date": ts}])[COLUMNS]
    merged = pd.concat([cur, new], ignore_index=True) if not cur.empty else new
    merged = dedupe_by_date(merged)
    _save_cached(qvix_type, merged)
    return {"ok": True, "date": row["date"], "close": row["close"],
            "rows": len(pairs), "time": t_last, "suspect": bool(suspect)}


# 补采时要求分钟文件的末值至少走到这个时刻，才算「一份完整的收盘序列」
CATCHUP_CLOSE_TIME = datetime.strptime("14:55", "%H:%M").time()


def catchup_prev_session(qvix_type: str, now: datetime | None = None,
                         calendar: set | None = None) -> dict:
    """开盘前补采**上一交易日**的收盘 QVIX（15:05 那轮没拿到时救回来）。

    背景（2026-11-09）：11-05 源站分钟文件整天没更新（与 11-04 逐点一致），
    15:05~16:15 连续拒落库 → 11-05 的官方值永久缺失，随后 5 个交易日的
    5 日动量全部算不出来（判定一直卡在「缺一天」）。而源站**事后**才更新
    分钟文件的情况确实存在（11-02 就是晚上才算出来的）。

    为什么这时补采不会张冠李戴：**开盘前（<09:25）**今天一根分钟值都还没有，
    文件里那份必然是上一个交易日的。所以只有在 09:25 之前才允许补采。

    守门（宁缺勿假）：
      - 库里已有该日 → 跳过；
      - 末值时刻 < 14:55 → 不是完整收盘序列，拒写；
      - 指纹与上一交易日完全相同 → 源站还是旧文件（拒写，同 `snapshot`）。
    """
    now = now or datetime.now()
    if now.time() >= datetime.strptime("09:25", "%H:%M").time():
        return {"ok": False, "reason": "已开盘，无法判断分钟文件属于哪一天，不补采"}

    target = last_trading_day_before(now.date(), calendar=calendar)
    if target is None:
        return {"ok": False, "reason": "交易日历不可用或找不到上一交易日"}

    cached = load_cached(qvix_type)          # suspect 行不算「已有」
    if not cached.empty and bool((cached["date"].dt.date == target).any()):
        return {"ok": False, "date": str(target), "reason": "库里已有该日，无需补采"}

    try:
        pairs = _fetch_minute_table(qvix_type)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "date": str(target), "reason": f"抓取失败: {e}"}
    if not pairs:
        return {"ok": False, "date": str(target), "reason": "分钟文件无有效值"}

    t_last, v_last = max(pairs, key=_minute_time)
    if _minute_time((t_last, v_last)) < CATCHUP_CLOSE_TIME:
        return {"ok": False, "date": str(target),
                "reason": f"末值时刻 {t_last} 未到收盘，疑似盘中文件"}

    fp = _fingerprint(pairs)
    prev, _trusted = _prev_row(qvix_type, target)
    if prev is not None:
        reason = _stale_snapshot_reason(prev, fp)
        if reason:
            return {"ok": False, "date": str(target), "reason": reason}

    row = {"date": str(target), "close": round(float(v_last), 4),
           "source": f"catchup@{t_last}",
           "captured_at": now.strftime("%Y-%m-%d %H:%M:%S"),
           "fp": fp, "suspect": ""}
    cur = _load_raw(qvix_type)
    ts = pd.Timestamp(target)
    if not cur.empty:
        cur = cur[cur["date"] != ts]
    new = pd.DataFrame([{**row, "date": ts}])[COLUMNS]
    merged = pd.concat([cur, new], ignore_index=True) if not cur.empty else new
    _save_cached(qvix_type, dedupe_by_date(merged))
    logger.warning(f"QVIX {qvix_type} {target}: 开盘前补采成功 close={v_last}"
                   f"（分钟行 {len(pairs)}，末值 {t_last}）")
    return {"ok": True, "date": row["date"], "close": row["close"],
            "rows": len(pairs), "time": t_last, "reason": "开盘前补采上一交易日"}


def drop_weekend_rows(df: pd.DataFrame, qvix_type: str = "") -> pd.DataFrame:
    """丢掉源站里日期落在周六/周日的行。

    2026-11-04 实测源站 k.csv 末尾出现了 `2026/11/1`（周日）一行 —— 星期几根本
    不可能有行情，这种行只会把日历对齐搞乱。周内的错值交给 `apply_verified` 判。
    """
    if df is None or df.empty or "date" not in df.columns:
        return df
    d = pd.to_datetime(df["date"], errors="coerce")
    bad = d.dt.dayofweek >= 5
    if not bad.any():
        return df
    dropped = d[bad].dt.strftime("%Y-%m-%d").tolist()
    logger.warning(f"QVIX {qvix_type}: 源站出现非交易日行，已剔除 {dropped}")
    return df[~bad].reset_index(drop=True)


# 源站值与本地实测值的偏离阈值（对数比）。正常「15:00:34 抓到的末值 vs 源站重算值」
# 差 0.1~0.7（≈0.3%~2%），8% 留足余量，只拦「重算出垃圾」这种量级的错。
VERIFIED_MAX_DEV = float(os.environ.get("QVIX_VERIFIED_MAX_DEV", "0.08"))


def repair_day(qvix_type: str, day, close: float | None, reason: str = "") -> dict:
    """写入/撤销**经交叉核对**的某一天 QVIX 值（source=verified，不算 suspect）。

    close=None → 删掉该日的 verified 行（核对发现原判断错了时用）。

    用途：源站事后补行但值算错（11-02 的 27.51），而本地这条日期又缺（当天假快照已
    被标 suspect）→ 判定永远卡在「QVIX 缺口」。此时把核对过的值写进来，`apply_verified`
    会让它**压过**源站的错值，缺口补上、判定恢复正常，且留痕可追溯。
    """
    ts = pd.Timestamp(day)
    cur = _load_raw(qvix_type)
    if not cur.empty:
        cur = cur[cur["date"] != ts]
    action = "removed"
    if close is not None:
        row = pd.DataFrame([{
            "date": ts, "close": round(float(close), 4),
            "source": "verified", "captured_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "fp": "", "suspect": "",
        }])[COLUMNS]
        cur = pd.concat([cur, row], ignore_index=True) if not cur.empty else row
        action = "written"
        logger.warning(f"QVIX {qvix_type} {ts:%Y-%m-%d}: 写入核对值 {close}（{reason or '无说明'}）")
    _save_cached(qvix_type, cur)
    return {"action": action, "date": f"{ts:%Y-%m-%d}", "close": close, "reason": reason}


def apply_verified(df: pd.DataFrame, qvix_type: str,
                   max_dev: float | None = None) -> pd.DataFrame:
    """源站日线与本地实测值差得离谱时，**以本地为准**。

    规则（只对两边都有的日期生效，suspect 行不参与）：
      - 本地 source=verified（`repair_day` 写的）→ 无条件覆盖源站；
      - 否则偏离 > max_dev（默认 8%）→ 覆盖源站并告警（源站重算出了垃圾）。
    正常日子源站值与 15:05 实测值差 <2%，不会被动到。
    """
    if df is None or df.empty or "date" not in df.columns or "close" not in df.columns:
        return df
    cache = _load_raw(qvix_type)
    if cache.empty:
        return df
    cache = cache[~_suspect_mask(cache)]
    if cache.empty:
        return df
    # proxy 行只是估算，**不能**拿来压源站的真实值（2026-11-09）：反过来才对，
    # 源站一有值就以源站为准，proxy 行在 dedupe_by_date 里本来也排在它后面。
    src = cache["source"].astype(str).str.strip().str.lower()
    cache = cache[~src.str.startswith("proxy")]
    if cache.empty:
        return df
    cache = cache.drop_duplicates(subset=["date"], keep="last").set_index("date")
    dev_limit = VERIFIED_MAX_DEV if max_dev is None else max_dev

    out = df.copy()
    out["date"] = pd.to_datetime(out["date"])
    fixed = 0
    for idx, row in out.iterrows():
        d = row["date"]
        if d not in cache.index:
            continue
        local = cache.loc[d]
        try:
            src_v = float(row["close"])
            loc_v = float(local["close"])
        except (TypeError, ValueError):
            continue
        if pd.isna(src_v) or pd.isna(loc_v) or src_v <= 0 or loc_v <= 0:
            continue
        verified = str(local.get("source", "")).startswith("verified")
        dev = abs(math.log(src_v / loc_v))
        if verified or dev > dev_limit:
            out.at[idx, "close"] = loc_v
            fixed += 1
            logger.warning(
                f"QVIX {qvix_type} {d:%Y-%m-%d}: 源站值 {src_v:.2f} 与本地"
                f"{'核对值' if verified else '实测值'} {loc_v:.2f} 不符"
                f"（偏离 {dev:.1%}），以本地为准")
    if fixed:
        logger.warning(f"QVIX {qvix_type}: 共修正 {fixed} 天源站可疑值")
    return out.reset_index(drop=True)


def overlay(df: pd.DataFrame, qvix_type: str) -> pd.DataFrame:
    """用本地库补 df 里的缺口（**只补空、不覆盖源站值**），并把源站还没有的日期补上。

    suspect 行（疑似源站未更新的陈旧值）默认不参与补 —— 宁可继续缺、让守门拒判，
    也不能拿昨天/周五的值冒充新值（2026-11-02 的假快照就是这么来的）。

    2026-11-04：源站日线会**晚几天**才补行（11-04 早上才补出 11-02，11-03 还没有）。
    只填 NaN 是不够的 —— 日期行根本不存在，`merge` 之后那一天照样是空的，5 日动量
    依旧算不出来。所以这里把本地库里有、源站没有的日期**整行追加**进去。
    """
    if df is None or df.empty or "date" not in df.columns or "close" not in df.columns:
        return df
    cache = _load_raw(qvix_type)
    if cache.empty:
        return df
    bad = _suspect_mask(cache)
    if bad.any():
        logger.info(f"QVIX {qvix_type}: 跳过 {int(bad.sum())} 条可疑(suspect)快照")
        cache = cache[~bad]
        if cache.empty:
            return df
    cache = cache.drop_duplicates(subset=["date"], keep="last")
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"])
    mapping = cache.set_index("date")["close"]
    filled = out["close"].fillna(out["date"].map(mapping))
    patched = int((filled.notna() & out["close"].isna()).sum())
    out["close"] = filled

    known = set(out["date"])
    extra = cache[~cache["date"].isin(known)][["date", "close"]]
    if not extra.empty:
        out = (pd.concat([out, extra], ignore_index=True)
                 .sort_values("date").reset_index(drop=True))
        logger.info(f"QVIX {qvix_type}: 本地自建库补了 {len(extra)} 天源站还没有的日期"
                    f"（{extra['date'].min():%Y-%m-%d}~{extra['date'].max():%Y-%m-%d}）")
    if patched:
        logger.info(f"QVIX {qvix_type}: 本地自建库补了 {patched} 天空缺")
    return out


def snapshot_all(for_date: date | None = None) -> dict:
    """所有品种各落一次（供 jobs/qvix_snapshot.py 调用）。"""
    result = {}
    for qt in MINUTE_CODES:
        result[qt] = snapshot(qt, for_date=for_date)
    return result
