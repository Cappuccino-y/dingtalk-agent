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


def fetch_qvix(qvix_type: str) -> pd.DataFrame:
    """
    拉 QVIX 日线。
    qvix_type ∈ {"50etf", "300etf", "500etf", "1000index"}
    返回列: date, open, high, low, close（百分比，已年化）
    """
    if qvix_type not in QVIX_FETCHERS:
        raise ValueError(f"Unknown qvix_type: {qvix_type}. Choose from {list(QVIX_FETCHERS)}")
    df = QVIX_FETCHERS[qvix_type]()
    df["date"] = pd.to_datetime(df["date"])
    # 清洗：剔除全 NaN 行 / close=0 行
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0]
    df = df.sort_values("date").reset_index(drop=True)
    df["qvix_type"] = qvix_type
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