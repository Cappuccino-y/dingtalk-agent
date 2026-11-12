"""
AkShare 数据获取封装。

可用数据源（已验证）：
- ✅ index_option_50etf_qvix / _300etf_qvix / _500etf_qvix / _1000index_qvix
     → QVIX（基于期权链计算的 VIX 替代品，免费、最直接）
- ✅ stock_zh_index_daily
     → 现货指数日线（中证1000/500/300/上证50）
- ✅ futures_zh_daily_sina(symbol="IM2206")
     → 单个 IM/IC 期货合约日线，需自己拼接主力连续
- ✅ futures_zh_minute_sina(symbol="IM2509", period="5")
     → 单合约 5 分钟 K 线（约 30 天历史）
- ✅ option_cffex_zz1000_spot_sina
     → IM 股指期权实时快照（call + put）

⚠️ eastmoney 数据源被代理拦截（option_current_em / futures_hist_em 不可用）
"""

from __future__ import annotations

import logging
import math
import os
import socket
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

import akshare as ak
import pandas as pd

logger = logging.getLogger(__name__)

# 屏蔽 akshare 内部的 pandas warning
import warnings
warnings.filterwarnings("ignore", category=UserWarning)


# ============================================================
# 1. 现货指数
# ============================================================

def fetch_spot_daily(
    symbol: str,           # 例: "sh000852"
    start_date: Optional[str] = None,
) -> pd.DataFrame:
    """
    拉现货指数日线。
    返回列: date, open, high, low, close, volume
    """
    df = ak.stock_zh_index_daily(symbol=symbol)
    df = df.rename(columns={"date": "date"})
    df["date"] = pd.to_datetime(df["date"])
    if start_date:
        df = df[df["date"] >= pd.to_datetime(start_date)]
    df = df.sort_values("date").reset_index(drop=True)
    return df


# ============================================================
# 2. QVIX（直接拿现成的波动率指数，省去重建 VIX 的麻烦）
# ============================================================

QVIX_FETCHERS = {
    "50etf":    ak.index_option_50etf_qvix,       # 2015-02-09 起，最长样本
    "300etf":   ak.index_option_300etf_qvix,      # 2019-12-23 起
    "500etf":   ak.index_option_500etf_qvix,      # 2022-09-19 起
    "1000index": ak.index_option_1000index_qvix,  # 2022-07-22 起，IM/IC 直接相关
}

# 源站（optbbs）的日线是一个 ~900KB 的 CSV，偶发下到一半断开（IncompleteRead）、
# 或者慢到几乎不动。重试次数与超时可用环境变量调；日线一天只需拉一次。
QVIX_FETCH_ATTEMPTS = int(os.environ.get("QVIX_FETCH_ATTEMPTS", "3"))
QVIX_CONNECT_TIMEOUT = float(os.environ.get("QVIX_CONNECT_TIMEOUT", "10"))
QVIX_READ_TIMEOUT = float(os.environ.get("QVIX_READ_TIMEOUT", "20"))
# 2026-11-04：45s 的总时限太紧 —— 早上源站限速到 ~20KB/s 时，920KB 的文件每次
# 都只差最后几十 KB 就被砍掉，三次全废，整天拒判。实测同一份文件直连 1.9s。
QVIX_TOTAL_TIMEOUT = float(os.environ.get("QVIX_TOTAL_TIMEOUT", "120"))
# 走系统代理（127.0.0.1:7890）比直连慢一倍，而且本机代理会**间歇性**掐 TLS，
# 这个站点在国内、不需要代理 → 默认绕开，需要时用 QVIX_USE_PROXY=1 打开。
QVIX_USE_PROXY = os.environ.get("QVIX_USE_PROXY", "0").strip() not in ("", "0", "false", "False")
OPT_BBS_DAILY_URL = "http://1.optbbs.com/d/csv/d/k.csv"
QVIX_MIN_BYTES = 100_000          # 正常 ~920KB；小于这个数一定是被截断


class _QvixParseUnavailable(RuntimeError):
    """akshare 内部结构变了（找不到 __get_optbbs_daily）→ 只能退回它自己的读法。"""


def _with_socket_timeout(fn, timeout: float):
    """在调用期间给 socket 设默认超时，结束后还原。

    akshare 的 optbbs 日线是 `pd.read_csv(url)`，走 urllib 且**不带 timeout**：
    源站半路不吭声时只能等 TCP 层超时（实测能挂 3 分钟）。这里只作为
    「akshare 内部结构变了」时的兜底。
    """
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        return fn()
    finally:
        socket.setdefaulttimeout(old)


def _download_optbbs_daily(connect_timeout: float | None = None,
                           read_timeout: float | None = None,
                           total_timeout: float | None = None) -> bytes:
    """自己把 optbbs 的日线 CSV 下下来（一次尝试），并校验没被截断。

    为什么不直接让 akshare 读 URL：它 `pd.read_csv(url)` 既没有 timeout，
    也不校验长度 —— 2026-11-02 09:20 就是下到 213KB/923KB 被断开，
    `IncompleteRead` 直接把整个 IC 作业打成 exit 1。

    除了每块读的超时，还要一个**总时限**：源站今天（11-02 下午）是
    「一直滴数据但极慢」，单靠 per-read 超时，一次尝试能跑 3 分钟。
    """
    import requests

    deadline = time.monotonic() + (total_timeout or QVIX_TOTAL_TIMEOUT)
    session = requests.Session()
    # trust_env=False → 不读 HTTP(S)_PROXY/NO_PROXY：本站国内直连更快更稳（见上方说明）
    session.trust_env = QVIX_USE_PROXY
    resp = session.get(
        OPT_BBS_DAILY_URL,
        timeout=(connect_timeout or QVIX_CONNECT_TIMEOUT,
                 read_timeout or QVIX_READ_TIMEOUT),
        stream=True,
    )
    resp.raise_for_status()
    chunks: list[bytes] = []
    size = 0
    for chunk in resp.iter_content(64 * 1024):
        if not chunk:
            continue
        chunks.append(chunk)
        size += len(chunk)
        if time.monotonic() > deadline:
            resp.close()
            raise IOError(f"下载超时（超过 {(total_timeout or QVIX_TOTAL_TIMEOUT):.0f}s，"
                          f"只拿到 {size} bytes）")
    data = b"".join(chunks)
    expected = resp.headers.get("Content-Length")
    if expected and expected.isdigit() and len(data) < int(expected):
        raise IOError(f"下载不完整（{len(data)}/{expected} bytes）")
    if len(data) < QVIX_MIN_BYTES:
        raise IOError(f"内容过短，疑似被截断（{len(data)} bytes）")
    return data


def _qvix_df_from_bytes(qvix_type: str, raw: bytes) -> pd.DataFrame:
    """把下好的 k.csv 喂给 akshare 自己的解析逻辑。

    akshare 的 index_option_*_qvix 内部都读模块级的 `__get_optbbs_daily()`；
    这里临时把它换成「读我们下好的字节」—— 列映射仍由 akshare 维护，
    我们只接管「怎么下载」，不复制它那套列下标（下标才是最容易变的东西）。
    """
    import io
    from akshare.index import index_option_qvix as aqv

    if not hasattr(aqv, "__get_optbbs_daily"):
        raise _QvixParseUnavailable("akshare 内部结构变了：找不到 __get_optbbs_daily")
    orig = aqv.__get_optbbs_daily
    try:
        aqv.__get_optbbs_daily = lambda: pd.read_csv(io.BytesIO(raw), encoding="gbk")
        return QVIX_FETCHERS[qvix_type]()
    finally:
        aqv.__get_optbbs_daily = orig


def _fetch_qvix_raw(qvix_type: str, attempts: int | None = None) -> pd.DataFrame:
    """拉一次 QVIX 日线，失败重试（默认 3 次，间隔 2s/4s）。

    2026-11-02：一次截断 / 一次卡住就重下一遍即可，源站本身没问题。
    """
    attempts = attempts or QVIX_FETCH_ATTEMPTS
    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            raw = _download_optbbs_daily()
            df = _qvix_df_from_bytes(qvix_type, raw)
            if df is None or df.empty:
                raise RuntimeError("akshare 返回空表")
            return df
        except _QvixParseUnavailable as e:
            # 解析入口不在了（akshare 升级）→ 退回它自己的读法，只试一次
            logger.warning(f"QVIX {qvix_type} {e}，改走 akshare 原始读取路径")
            df = _with_socket_timeout(QVIX_FETCHERS[qvix_type],
                                      QVIX_READ_TIMEOUT * 2)
            if df is not None and not df.empty:
                return df
            last = e
            break
        except Exception as e:  # noqa: BLE001
            last = e
            logger.warning(f"QVIX {qvix_type} 第 {i}/{attempts} 次拉取失败：{e}")
            if i < attempts:
                time.sleep(min(2 * i, 5))
    raise RuntimeError(f"QVIX {qvix_type} 连续 {attempts} 次拉取失败：{last}") from last


def fetch_qvix(qvix_type: str, use_cache: bool = True) -> pd.DataFrame:
    """
    拉 QVIX 日线。
    qvix_type ∈ {"50etf", "300etf", "500etf", "1000index"}
    返回列: date, open, high, low, close（百分比，已年化）

    use_cache=True 时用本地自建库（var/qvix/）补源站回填滞后的空缺。
    背景见 datalayer/qvix_store.py：optbbs 日线的行是按日历预建的，值事后回填，
    2026-10-23 那次源站连续两天没回填，直接把 IC 信号打成「拒判」。

    2026-11-02：源站整体不可用（重试全败）时**退回本地全量快照**，
    不再让一次网络抖动把整个 IC 作业打成 exit 1；连快照都没有才抛异常。
    """
    if qvix_type not in QVIX_FETCHERS:
        raise ValueError(f"Unknown qvix_type: {qvix_type}. Choose from {list(QVIX_FETCHERS)}")

    try:
        from datalayer.qvix_store import (apply_verified, drop_weekend_rows,
                                          load_cached, load_full, overlay,
                                          save_full)
    except ImportError:  # 兼容 src/utils 老路径
        from qvix_store import (apply_verified, drop_weekend_rows,  # type: ignore
                                load_cached, load_full, overlay, save_full)

    source = "optbbs"
    try:
        df = _fetch_qvix_raw(qvix_type)
    except Exception as e:  # noqa: BLE001
        # 源站挂了：拿本地两份数据兜底 —— 整段日线快照（历史）+ 15:05 收盘快照
        # （最新日真实收盘值）。两份都空才认输，避免「源站一慢就整天拒判」。
        parts = [p for p in (load_full(qvix_type), load_cached(qvix_type))
                 if p is not None and not p.empty]
        fallback = (pd.concat(parts, ignore_index=True)
                    if parts else pd.DataFrame(columns=["date", "close"]))
        if not fallback.empty:
            fallback = (fallback.drop_duplicates(subset=["date"], keep="last")
                        .sort_values("date").reset_index(drop=True))
        if fallback.empty:
            logger.error(f"QVIX {qvix_type} 源站不可用且无本地数据，放弃：{e}")
            raise
        logger.warning(f"QVIX {qvix_type} 源站不可用，改用本地数据"
                       f"（{len(fallback)} 行，最后 {fallback['date'].iloc[-1]:%Y-%m-%d}）：{e}")
        df = fallback
        source = "local"

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])

    # 源站成功时先剔除周六/周日行、再用本地核对值压掉源站的错值，最后才落快照
    # （落进全量快照的必须是干净数据，否则以后拿它兜底会带进垃圾）。
    if source == "optbbs":
        try:
            df = drop_weekend_rows(df, qvix_type)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"QVIX {qvix_type} 非交易日过滤失败（忽略）: {e}")
    try:
        df = apply_verified(df, qvix_type)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"QVIX {qvix_type} 核对值覆盖失败（忽略）: {e}")

    # 2026-11-09 修：源站的 k.csv 会给**同一天**吐两行（11-02 就是 27.51 与 33.81
    # 各一行，前者是它事后拿残缺期权链重算的垃圾值）。以前完全没去重 —— 重复行
    # 进了 df 之后，`_mom_with_gap_guard` / `pct_change` 这类**按位置位移**的窗口
    # 会整体错位一格（"5 天前"其实落到 4 天或 6 天前），判定口径再严也白搭。
    # 这里按日期去重（核对值已在上面覆盖过，所以两行会收敛到同一个正确值）。
    if "date" in df.columns:
        dup = df["date"][df["date"].duplicated(keep=False)]
        if not dup.empty:
            dups = sorted({f"{d:%Y-%m-%d}" for d in dup})
            # 留哪一行不看"谁排在后面"（源站会把错值追加在后面），而是**贴着本地
            # 实测/核对值**留 —— 本地 15:05 快照和 verified 行才是真收盘价。
            local = load_cached(qvix_type)
            anchor = {} if local.empty else {
                pd.Timestamp(d): float(v) for d, v in zip(local["date"], local["close"])}
            keep_rows = []
            for d, g in df.groupby("date", sort=False):
                if len(g) > 1 and d in anchor and anchor[d] > 0:
                    def _dev(v):
                        try:
                            v = float(v)
                            return abs(math.log(v / anchor[d])) if v > 0 else float("inf")
                        except (TypeError, ValueError):
                            return float("inf")
                    keep_rows.append(g["close"].apply(_dev).idxmin())
                else:
                    keep_rows.append(g.index[-1])
            logger.warning(f"QVIX {qvix_type}: 源站出现重复日期 {dups}，按日期去重")
            df = (df.loc[sorted(keep_rows)].sort_values("date").reset_index(drop=True))

    if source == "optbbs":
        try:
            save_full(qvix_type, df)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"QVIX {qvix_type} 全量快照落盘失败（忽略）: {e}")

    if use_cache:
        try:
            df = overlay(df, qvix_type)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"QVIX {qvix_type} 本地缓存补洞失败（忽略）: {e}")
    # 清洗：剔除全 NaN 行 / close=0 行
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0]
    df = df.sort_values("date").reset_index(drop=True)
    df["qvix_type"] = qvix_type
    if source != "optbbs":
        logger.info(f"QVIX {qvix_type} 本次数据来自{source}")
    return df


def fetch_all_qvix() -> pd.DataFrame:
    """拉所有 QVIX 拼成一个表（每行带 qvix_type）。"""
    frames = []
    for qt in QVIX_FETCHERS:
        try:
            frames.append(fetch_qvix(qt))
        except Exception as e:
            logger.warning(f"QVIX {qt} 拉取失败: {e}")
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ============================================================
# 3. 期货主力连续（拼接所有月度合约）
# ============================================================

# 中金所股指期货合约月份是数字 (01-12 排除某些月份)，格式 IM2509 = IM + YY + MM
# 实际合约月份：G(6) F(1) H(3) J(5) K(7) M(8) N(9) Q(11) U(12) V(13) X(11) Z(12)
# 不对，实际数字就是月份数字 01-12
CFFEX_FUTURES_MONTHS = ["01", "02", "03", "04", "05", "06",
                        "07", "08", "09", "10", "11", "12"]


def _contract_codes(prefix: str, year_start: int, year_end: int) -> List[str]:
    """
    生成所有月度合约代码。
    格式: prefix + YY(年) + MM(月)
    例: _contract_codes("IM", 2022, 2026) → ['IM2201', 'IM2202', ..., 'IM2612']
    """
    codes = []
    for y in range(year_start, year_end + 1):
        yy = str(y)[-2:]              # 2 位年（如 22, 23, 24, 25, 26）
        for mm in CFFEX_FUTURES_MONTHS:
            codes.append(f"{prefix}{yy}{mm}")
    return codes


def fetch_futures_daily(
    code_prefix: str,         # 例: "IM" / "IC"
    year_start: int,
    year_end: Optional[int] = None,
    progress: bool = True,
) -> pd.DataFrame:
    """
    拉期货主力连续日线（拼接所有合约）。
    返回列: date, open, high, low, close, volume, open_interest, settle, contract
    """
    if year_end is None:
        year_end = datetime.now().year

    codes = _contract_codes(code_prefix, year_start, year_end)
    frames = []
    failed = []

    for i, code in enumerate(codes):
        if progress:
            logger.info(f"[{i+1}/{len(codes)}] 拉取 {code} ...")
        try:
            df = ak.futures_zh_daily_sina(symbol=code)
            # akshare 在某些合约上会返回空 df 或抛 Length mismatch，统统跳过
            if df is None or df.empty or len(df) < 5:
                continue
            df["contract"] = code
            df["date"] = pd.to_datetime(df["date"])
            frames.append(df)
            time.sleep(0.3)
        except Exception as e:
            failed.append(code)
            logger.debug(f"{code} 失败: {type(e).__name__}: {str(e)[:80]}")

    if not frames:
        raise RuntimeError(f"未拉到任何 {code_prefix} 合约数据")

    raw = pd.concat(frames, ignore_index=True)
    raw = raw.sort_values(["date", "contract"]).reset_index(drop=True)

    # 主力连续：每个交易日取成交量最大的合约（对中金所股指期货足够近似）
    vol_idx = raw.groupby("date")["volume"].idxmax()
    main = raw.loc[vol_idx].sort_values("date").reset_index(drop=True)

    main = main.rename(columns={"hold": "open_interest"})
    main = main[["date", "open", "high", "low", "close",
                 "volume", "open_interest", "settle", "contract"]]

    if failed and progress:
        logger.warning(f"失败的合约: {len(failed)} 个（样例: {failed[:5]}）")

    return main


# ============================================================
# 4. 期货分钟数据
# ============================================================

def fetch_futures_minute(
    code: str,
    period: str = "5",       # "1", "5", "15", "30", "60"
) -> pd.DataFrame:
    """
    拉单个合约的分钟 K 线（约 30 个交易日历史）。
    返回列: datetime, open, high, low, close, volume, hold
    """
    df = ak.futures_zh_minute_sina(symbol=code, period=period)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.sort_values("datetime").reset_index(drop=True)
    df["contract"] = code
    return df


# ============================================================
# 5. 中金所 IM/IC 期权实时快照
# ============================================================

def fetch_im_options_spot() -> pd.DataFrame:
    """
    拉中金所中证 1000 股指期权实时快照（call + put 一行对应）。
    返回列约 17 个，含 call/put 的开高低收、成交量、持仓量、行权价等。
    注意：返回的是中文列名，需要后续解析。
    """
    df = ak.option_cffex_zz1000_spot_sina()
    return df


# ============================================================
# 缓存 / 持久化
# ============================================================

def save_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    logger.info(f"已保存 {len(df)} 行到 {path}")


def load_parquet(path: Path) -> Optional[pd.DataFrame]:
    if not path.exists():
        return None
    return pd.read_parquet(path)


def with_cache(
    cache_path: Path,
    fetch_fn: Callable[[], pd.DataFrame],
    force_refresh: bool = False,
) -> pd.DataFrame:
    """有缓存就用缓存，否则 fetch。"""
    if not force_refresh:
        cached = load_parquet(cache_path)
        if cached is not None:
            logger.info(f"命中缓存: {cache_path} ({len(cached)} 行)")
            return cached
    df = fetch_fn()
    save_parquet(df, cache_path)
    return df