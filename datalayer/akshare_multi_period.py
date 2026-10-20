"""
akshare_multi_period.py — 中证1000 多周期指数数据 fetcher (akshare 替换版)

B 任务交付：基于 akshare 1.18.63 实测后只接入"可用部分"
- 日线 (daily): 走 akshare.stock_zh_index_daily → finance.sina.com.cn ✅ (实测 2873 行)
- 分钟线 (5m/15m/30m/60m): 走 akshare.index_zh_a_hist_min_em → push2his.eastmoney.com ❌ (与 efinance 同源 RST)

【关键实测结论 — 必须告诉读者】
akshare 1.18.63 中所有指数 em 后缀的接口（index_zh_a_hist_min_em / stock_zh_index_daily_em），
其底层 URL 都是 https://push2his.eastmoney.com/api/qt/stock/kline/get 与
https://push2his.eastmoney.com/api/qt/stock/trends2/get —— 和 efinance 完全同源。
本次 push2his 服务端 CDN 限流（RST, schannel: server closed abruptly (missing close_notify)），
akshare 没有"绕开路径" — Scout 提到的 webguest/api/ 上游 akshare 1.18.63 尚未合入。

【替换 trade_data_fetcher 中 _get_kline_efinance 的策略】
1. 日线 fallback 已通：sina 接口提供 2873 行历史数据，可完整接管原日线获取
2. 分钟线 4 个周期维持原代码路径（等待 push2his CDN 恢复），但加显式 None 守卫与降级日志
3. 返回的 dict 结构与 trade_data_fetcher.get_multi_period_data 完全一致：
   {period_name(str): pd.DataFrame(columns=[日期,开,高,低,收,成交量, ...])}

【用法】
from akshare_multi_period import AkshareMultiPeriodFetcher
f = AkshareMultiPeriodFetcher()
data = f.get_multi_period_data("sh000852", ["5min","15min","30min","60min","日线"])
"""
from __future__ import annotations
import logging
from typing import Dict, List, Optional, Sequence

import pandas as pd

logger = logging.getLogger(__name__)

# 中证 1000 在 akshare 的 symbol 编码
INDEX_SYMBOL_MAP: Dict[str, str] = {
    "中证1000": "sh000852",
    "上证指数": "sh000001",
    "沪深300": "sh000300",
    "中证500": "sh000905",
    "创业板指": "sz399006",
    "科创50":  "sh000688",
    "上证50":  "sh000016",
    "深证成指": "sz399001",
    "北证50":  "bj899050",
}


class AkshareMultiPeriodFetcher:
    """
    akshare 多周期 fetcher — 仅替换数据源，方法签名对齐
    trade_data_fetcher.IndexDataFetcher.get_multi_period_data

    Attributes
    ----------
    index_name_map : Dict[str, str]
        中文名 → akshare symbol 映射（如 "中证1000" -> "sh000852"）
    daily_history_days : int
        日线历史默认拉取窗口（默认 30 年 = 10000 天 ~ 实际接口限制 ~ 2873 行 = ~12 年）
    """

    def __init__(
        self,
        index_name_map: Optional[Dict[str, str]] = None,
        proxy_disabled: bool = True,
    ) -> None:
        self.index_name_map = index_name_map or dict(INDEX_SYMBOL_MAP)
        # 与 trade_data_fetcher 保持一致，避免进入代理绕路
        self.proxy_disabled = proxy_disabled
        if proxy_disabled:
            self._disable_system_proxy()
        logger.info(
            "[AkshareMultiPeriodFetcher] ready (akshare path, sina daily / em minute-em is RST-blocked)"
        )

    @staticmethod
    def _disable_system_proxy() -> None:
        """强制不走系统代理 — 同 efinance 走 push2his 时的处理，确保统一行为"""
        import os
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
            os.environ.pop(k, None)

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------
    def get_multi_period_data(
        self,
        index_name: str = "中证1000",
        period_list: Sequence[str] = ("5min", "15min", "30min", "60min", "日线"),
    ) -> Dict[str, pd.DataFrame]:
        """
        拉取多周期指数数据，返回 dict[period, DataFrame]
        — 与 trade_data_fetcher.IndexDataFetcher.get_multi_period_data 签名一致
        — 单个周期失败返回空 DataFrame 而不是 None，便于无缝替换
        """
        symbol = self.index_name_map.get(index_name)
        if not symbol:
            logger.warning(f"[warn] 指数 {index_name} 不在 index_name_map 中，跳过")
            return {}

        result: Dict[str, pd.DataFrame] = {}
        for period in period_list:
            df = self._fetch_one(symbol, period)
            if df is not None and not df.empty:
                result[period] = df
            else:
                # 用空 DataFrame 占位（保持 dict 完整），并把 None 守卫落实
                result[period] = pd.DataFrame()
                logger.warning(f"[warn] {index_name} {period} 数据为空 — 降级空 DF")

        return result

    # ------------------------------------------------------------------
    # 内部：单周期抓取
    # ------------------------------------------------------------------
    def _fetch_one(self, symbol: str, period: str) -> Optional[pd.DataFrame]:
        """按周期路由到对应接口"""
        if period == "日线":
            return self._fetch_daily(symbol)
        if period in ("5min", "15min", "30min", "60min", "1min"):
            return self._fetch_minute(symbol, period)
        if period == "周线":
            # 周线 trade_data_fetcher 走 klt=102（push2his），当前 RST；用日线 resample 兜底
            return self._fetch_weekly_via_daily_resample(symbol)
        logger.warning(f"[warn] 不支持的周期: {period}")
        return None

    def _fetch_daily(self, symbol: str) -> Optional[pd.DataFrame]:
        """日线：ak.stock_zh_index_daily → sina (已验证)"""
        import akshare as ak
        try:
            df = ak.stock_zh_index_daily(symbol=symbol)
            if df is None or df.empty:
                return None
            # sina 列: date, open, close, high, low, volume → 标准化为中文列
            return self._standardize_columns(df, date_col="date")
        except Exception as e:
            logger.warning(f"[akshare] 日线抓取失败 {symbol}: {type(e).__name__}: {e}")
            return None

    def _fetch_minute(self, symbol: str, period: str) -> Optional[pd.DataFrame]:
        """分钟线：ak.index_zh_a_hist_min_em → push2his (当前 RST)"""
        import akshare as ak
        # 注：akshare 1.18.63 中此接口走 push2his.eastmoney.com/api/qt/stock/kline/get
        # 当 push2his 故障解除后会自通；当前会抛 ReadTimeout/ConnectionError
        try:
            df = ak.index_zh_a_hist_min_em(symbol=symbol, period=period.replace("min", ""))
            if df is None or df.empty:
                return None
            return self._standardize_columns(df, date_col="时间")
        except Exception as e:
            logger.warning(
                f"[akshare] {period} 抓取失败 {symbol}: {type(e).__name__}: "
                f"({str(e)[:120]}...) — push2his 当前 RST，等 CDN 恢复后自通"
            )
            return None

    def _fetch_weekly_via_daily_resample(self, symbol: str) -> Optional[pd.DataFrame]:
        """周线：当前 push2his RST 时，先用日线 resample 兜底生成周线"""
        daily = self._fetch_daily(symbol)
        if daily is None or daily.empty:
            return None
        try:
            daily_idx = daily.copy()
            daily_idx["日期"] = pd.to_datetime(daily_idx["日期"])
            daily_idx = daily_idx.set_index("日期")
            # OHLCV 周线规则
            agg = {
                "开":   "first",
                "高":   "max",
                "低":   "min",
                "收":   "last",
                "成交量": "sum",
            }
            weekly = daily_idx.resample("W").agg(agg).dropna()
            weekly = weekly.reset_index()
            weekly["日期"] = weekly["日期"].dt.strftime("%Y-%m-%d")
            return weekly
        except Exception as e:
            logger.warning(f"[akshare] 周线 resample 失败 {symbol}: {e}")
            return None

    # ------------------------------------------------------------------
    # 列名标准化（中文 → 目标列名）
    # ------------------------------------------------------------------
    @staticmethod
    def _standardize_columns(df: pd.DataFrame, date_col: str) -> pd.DataFrame:
        """将 akshare 各接口不同列名统一成 ['日期','开','高','低','收','成交量']"""
        rename_map = {
            "date": "日期",
            "时间": "日期",
            "open": "开", "开盘": "开",
            "high": "高", "最高": "高",
            "low":  "低", "最低": "低",
            "close": "收", "收盘": "收",
            "volume": "成交量",
        }
        out = df.rename(columns=rename_map).copy()

        # 确保 6 列齐全
        required = ["日期", "开", "高", "低", "收", "成交量"]
        for col in required:
            if col not in out.columns:
                out[col] = None

        # 数值列强转
        for col in ["开", "高", "低", "收", "成交量"]:
            out[col] = pd.to_numeric(out[col], errors="coerce")

        # 日期列转字符串
        if pd.api.types.is_datetime64_any_dtype(out["日期"]):
            out["日期"] = out["日期"].dt.strftime("%Y-%m-%d")
        else:
            out["日期"] = out["日期"].astype(str)

        return out[["日期", "开", "高", "低", "收", "成交量"]]


# ============================================================================
# 兼容性接入：让 trade_data_fetcher 的调用方可以无缝切到这个 fetcher
# ============================================================================
def get_multi_period_data(
    index_name: str = "中证1000",
    period_list: Sequence[str] = ("5min", "15min", "30min", "60min", "日线"),
) -> Dict[str, pd.DataFrame]:
    """函数式入口，方便直接替换 trade_data_fetcher.IndexDataFetcher.get_multi_period_data"""
    return AkshareMultiPeriodFetcher().get_multi_period_data(index_name, period_list)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    print("=" * 70)
    print("aksshare_multi_period 实跑验证 — 中证1000 (sh000852)")
    print("=" * 70)

    f = AkshareMultiPeriodFetcher()
    data = f.get_multi_period_data("中证1000", ["5min", "15min", "30min", "60min", "日线"])

    print()
    print(f"{'周期':<8s} {'行数':>6s} {'首行日期':<12s} {'末行日期':<12s} {'收价首/末':<14s}")
    print("-" * 70)
    for period in ["5min", "15min", "30min", "60min", "日线"]:
        df = data.get(period)
        if df is None or df.empty:
            print(f"{period:<8s} {'空':>6s}")
        else:
            first = df.iloc[0]
            last = df.iloc[-1]
            print(
                f"{period:<8s} {len(df):>6d} "
                f"{first['日期']:<12s} {last['日期']:<12s} "
                f"{first['收']:.2f}/{last['收']:.2f}"
            )

    # 输出每个周期抓取时实际 hit 的 URL（用于诊断）
    print()
    print("(诊断信息已写入 logger.INFO / WARNING 行)")
