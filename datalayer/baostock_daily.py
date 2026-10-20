import os

# 启用东方财富反爬补丁 (ENABLE_EASTMONEY_PATCH)
# 必须在 import efinance 之前启用
os.environ['no_proxy'] = 'push2his.eastmoney.com,push2.eastmoney.com,fund.eastmoney.com,anonflow2.eastmoney.com'

# 添加 trade 目录到 Python 路径以导入 eastmoney_patch
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eastmoney_patch import enable_eastmoney_patch
enable_eastmoney_patch()

# 8/14: 复用 trade 侧已验证的指标计算与磁盘缓存工具（同一套 A股参数 MACD/BOLL）
from trade_data_fetcher import (
    calc_macd_boll as calculate_macd_boll,
    analyze_macd_boll_state as analyze_macd_boll,
    load_disk_cache,
    save_disk_cache,
    merge_klines,
    clear_disk_cache,
)

import pandas as pd
import numpy as np
import re
from datetime import datetime, timedelta
from typing import Dict, Optional, List
import time
import requests
import logging

try:
    import efinance as ef
    EFINANCE_AVAILABLE = True
except ImportError:
    EFINANCE_AVAILABLE = False
    print("错误：efinance 未安装，请执行: pip install efinance")

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False
    print("警告：yfinance 未安装，亚洲指数功能不可用。安装命令: pip install yfinance")


class IndexDataFetcher:
    """基于 efinance 的指数多周期数据获取器，直接获取真实指数K线"""

    # 指数名称映射（efinance 可以直接使用中文名称）
    INDEX_NAME_MAP = {
        "中证1000": "中证1000",
        "上证指数": "上证指数",
    }

    # 周期配置：efinance 的 klt 参数值
    PERIOD_CONFIG = {
        "5min": {"klt": 5, "days_back": 5},
        "15min": {"klt": 15, "days_back": 7},
        "30min": {"klt": 30, "days_back": 15},
        "60min": {"klt": 60, "days_back": 30},
        "日线": {"klt": 101, "days_back": 180},
        "周线": {"klt": 102, "days_back": 365 * 2},
    }

    def __init__(self):
        if not EFINANCE_AVAILABLE:
            raise ImportError("efinance 未安装，无法使用本类")
        # 内存 TTL 缓存（与 trade_data_fetcher 同策略；磁盘缓存跨进程复用）
        self._cache = {}
        self._CACHE_TTL_SECONDS_INTRADAY = 60
        self._CACHE_TTL_SECONDS_SLOW = 300
        self._CACHE_MAX_ENTRIES = 200

    def _get_kline_efinance(self, index_name: str, frequency: str) -> Optional[pd.DataFrame]:
        """使用 efinance 获取指定指数的K线数据"""
        if index_name not in self.INDEX_NAME_MAP:
            print(f"未配置的指数名称: {index_name}")
            return None

        config = self.PERIOD_CONFIG.get(frequency)
        if not config:
            print(f"不支持的周期: {frequency}")
            return None

        klt = config["klt"]
        days_back = config["days_back"]

        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y%m%d")

        try:
            # 直接使用指数中文名称获取数据
            df = ef.stock.get_quote_history(
                stock_codes=index_name,
                klt=klt,
                beg=start_date,
                end=end_date
            )

            if df is None or df.empty:
                print(f"efinance 未返回 {index_name} {frequency} 数据，尝试腾讯备用源")
                return self._get_kline_tencent(index_name, frequency)

            # 列名统一映射（新增换手率）
            column_map = {
                "日期": "datetime",
                "开盘": "open",
                "最高": "high",
                "最低": "low",
                "收盘": "close",
                "成交量": "volume",
                "成交额": "amount",
                "换手率": "turnover_rate",  # 新增
            }
            df.rename(columns=column_map, inplace=True)

            # 保留需要的列（加入 turnover_rate）
            needed_cols = ['datetime', 'open', 'high', 'low', 'close', 'volume', 'amount', 'turnover_rate']
            df = df[[c for c in needed_cols if c in df.columns]]

            # 转换数据类型（turnover_rate 为 float）
            for col in ['open', 'high', 'low', 'close', 'volume', 'amount', 'turnover_rate']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')

            # 转换数据类型
            for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')

            # 时间列处理
            df['datetime'] = pd.to_datetime(df['datetime'])
            df['date'] = df['datetime'].dt.strftime('%Y-%m-%d')

            # 按时间排序
            df.sort_values('datetime', inplace=True)
            df.reset_index(drop=True, inplace=True)

            print(f"[efinance] 获取 {index_name} {frequency} 真实指数数据，共 {len(df)} 条")
            return df

        except Exception as e:
            print(f"[efinance] 获取 {index_name} {frequency} 失败: {e}")
            print(f"[efinance] 尝试腾讯备用数据源...")
            return self._get_kline_tencent(index_name, frequency)

    # ==================== 腾讯备用源（与 trade 侧一致，防东财限流中断报告） ====================
    _TENCENT_INDEX_MAP = {"中证1000": "sh000852", "上证指数": "sh000001"}
    _TENCENT_PERIOD_MAP = {
        "5min": ("m5", 5),
        "15min": ("m15", 15),
        "30min": ("m30", 30),
        "60min": ("m60", 60),
        "日线": ("day", None),
        "周线": ("week", None),
    }

    def _get_kline_tencent(self, index_name: str, frequency: str) -> Optional[pd.DataFrame]:
        """腾讯财经备用源（mkline/fqkline，无访问限制）"""
        symbol = self._TENCENT_INDEX_MAP.get(index_name)
        spec = self._TENCENT_PERIOD_MAP.get(frequency)
        if not symbol or not spec:
            print(f"[tencent] 未配置 {index_name} {frequency} 的备用源映射")
            return None
        try:
            tencent_period, minutes = spec
            if minutes:  # 分钟线：mkline 单次上限 320 根
                count = 320
                url = (f"https://ifzq.gtimg.cn/appstock/app/kline/mkline"
                       f"?param={symbol},{tencent_period},,{count}")
            else:  # 日线/周线：fqkline
                config = self.PERIOD_CONFIG.get(frequency, {})
                days_back = config.get("days_back", 180)
                count = min(days_back * 2, 1000)
                beg = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
                end = datetime.now().strftime("%Y-%m-%d")
                url = (f"https://ifzq.gtimg.cn/appstock/app/fqkline/get"
                       f"?param={symbol},{tencent_period},{beg},{end},{count},qfq")

            resp = requests.get(url, timeout=15, headers={
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
            })
            resp.raise_for_status()
            bars = resp.json()["data"][symbol].get(tencent_period) or []
            if not bars:
                print(f"[tencent] {index_name} {frequency} 无数据返回")
                return None

            rows = []
            for bar in bars:
                # 腾讯列序: [时间, 开, 收, 高, 低, 量, ...]
                dt = (pd.to_datetime(bar[0], format="%Y%m%d%H%M") if minutes
                      else pd.to_datetime(bar[0]))
                rows.append([
                    dt, float(bar[1]), float(bar[3]), float(bar[4]), float(bar[2]), float(bar[5])
                ])

            df = pd.DataFrame(rows, columns=["datetime", "open", "high", "low", "close", "volume"])
            df["date"] = df["datetime"].dt.strftime("%Y-%m-%d")
            df.sort_values("datetime", inplace=True)
            df.reset_index(drop=True, inplace=True)
            print(f"[tencent] 获取 {index_name} {frequency} 备用数据源成功，共 {len(df)} 条")
            return df
        except Exception as e:
            print(f"[tencent] 获取 {index_name} {frequency} 失败: {e}")
            return None

    def get_kline_data(self, index_name: str, frequency: str) -> Optional[pd.DataFrame]:
        """统一入口，获取指定周期的K线数据

        8/14: 内存 TTL + 跨进程磁盘缓存（data_cache/kline/report/），
        多次提问不重复全量拉取；过期时「缓存历史+新数据」拼接保留指标预热。
        """
        now_ts = time.time()
        cache_key = (index_name, frequency, datetime.now().strftime('%Y%m%d'))
        ttl = (self._CACHE_TTL_SECONDS_INTRADAY
               if frequency in ("5min", "15min", "30min", "60min")
               else self._CACHE_TTL_SECONDS_SLOW)

        cached = self._cache.get(cache_key)
        if cached and now_ts - cached[0] < ttl:
            return cached[1].copy()

        disk_df = load_disk_cache(index_name, frequency, ttl_seconds=ttl, namespace="report")
        if disk_df is not None and len(disk_df) > 0:
            self._cache[cache_key] = (now_ts, disk_df)
            return disk_df.copy()

        df = self._get_kline_efinance(index_name, frequency)
        if df is not None:
            df = merge_klines(disk_df, df)
            save_disk_cache(index_name, frequency, df, namespace="report")
            self._cache[cache_key] = (now_ts, df)
            if len(self._cache) > self._CACHE_MAX_ENTRIES:
                oldest_key = min(self._cache, key=lambda k: self._cache[k][0])
                del self._cache[oldest_key]
            return df.copy()
        return None

    def clear_cache(self) -> None:
        """清空缓存（强制重新拉取）"""
        self._cache.clear()
        clear_disk_cache()

    # ==================== 技术指标计算（保持不变）====================
    def calculate_ma(self, df: pd.DataFrame, periods: List[int] = [5, 10, 20, 60]) -> pd.DataFrame:
        df = df.copy()
        for period in periods:
            df[f'MA{period}'] = df['close'].rolling(window=period).mean()
        return df

    def calculate_volume_ma(self, df: pd.DataFrame, period: int = 5) -> pd.DataFrame:
        df = df.copy()
        df[f'Volume_MA{period}'] = df['volume'].rolling(window=period).mean()
        return df

    def get_volume_ratio(self, df: pd.DataFrame, short_period: int = 5) -> pd.DataFrame:
        df = df.copy()
        df['Volume_MA'] = df['volume'].rolling(window=short_period).mean().shift(1)
        df['Volume_Ratio'] = df['volume'] / df['Volume_MA']
        return df

    def analyze_trend(self, df: pd.DataFrame, period: str = "日线") -> Dict[str, str]:
        """
        技术面综合分析
        :param df: 包含OHLCV及指标列的DataFrame
        :param period: 周期标识，用于调整敏感度阈值
        :return: 结构化分析结果字典
        """
        if df.empty or len(df) < 2:
            return {}

        latest = df.iloc[-1]
        prev = df.iloc[-2]
        analysis = {}

        # ==================== 跨周期敏感度配置 ====================
        if period in ["5min", "15min", "30min", "60min"]:
            gap_threshold = 0.15
            vol_high = 1.3
            vol_low = 0.7
            ma_spread_threshold = 1.0
            price_move_threshold = 0.15  # ← 新增：涨跌幅阈值（分钟线）
        elif period == "日线":
            gap_threshold = 0.3
            vol_high = 1.5
            vol_low = 0.5
            ma_spread_threshold = 2.0
            price_move_threshold = 0.5  # ← 新增：涨跌幅阈值（日线）
        else:  # 周线
            gap_threshold = 0.5
            vol_high = 1.5
            vol_low = 0.5
            ma_spread_threshold = 3.0
            price_move_threshold = 1.0  # ← 新增：涨跌幅阈值（周线）

        # ==================== 1. 基础涨跌 ====================
        change_pct = (latest['close'] - prev['close']) / prev['close'] * 100
        analysis['change_pct'] = f"{change_pct:+.2f}%"
        analysis['trend'] = "上涨" if change_pct > 0 else "下跌"

        # ==================== 2. 跳空缺口检测 ====================
        gap_pct = (latest['open'] - prev['close']) / prev['close'] * 100
        if abs(gap_pct) > gap_threshold:
            analysis['gap'] = f"{'高开' if gap_pct > 0 else '低开'}{abs(gap_pct):.2f}%"

        # ==================== 3. 均线位置分析 ====================
        # 均线列筛选：严格匹配 MA+数字（MACD 列加入后 startswith('MA') 会把 MACD_HIST 误判为均线）
        ma_columns = [col for col in df.columns if re.match(r'^MA\d+$', col)]
        for ma in ma_columns:
            if pd.notna(latest[ma]):
                if latest['close'] > latest[ma]:
                    analysis[f'{ma}_position'] = "站上"
                elif latest['close'] < latest[ma]:
                    analysis[f'{ma}_position'] = "跌破"
                else:
                    analysis[f'{ma}_position'] = "持平"

        # ==================== 4. 均线排列与发散/收敛 ====================
        ma_values = [(ma, latest[ma]) for ma in sorted(ma_columns, key=lambda x: int(x[2:])) if pd.notna(latest[ma])]

        if len(ma_values) >= 3:
            ma_nums = [v for _, v in ma_values]
            if all(ma_nums[i] > ma_nums[i + 1] for i in range(len(ma_nums) - 1)):
                analysis['ma_arrangement'] = "多头排列"
            elif all(ma_nums[i] < ma_nums[i + 1] for i in range(len(ma_nums) - 1)):
                analysis['ma_arrangement'] = "空头排列"
            else:
                analysis['ma_arrangement'] = "均线缠绕"
        else:
            analysis['ma_arrangement'] = "数据不足"

        # 均线发散/收敛程度
        if len(ma_values) >= 2:
            ma_short = ma_values[0][1]  # 最短周期均线
            ma_long = ma_values[-1][1]  # 最长周期均线
            spread_pct = (ma_short - ma_long) / ma_long * 100

            if abs(spread_pct) < ma_spread_threshold * 0.5:
                analysis['ma_spread'] = "极度粘合"
            elif abs(spread_pct) < ma_spread_threshold:
                analysis['ma_spread'] = "粘合"
            elif spread_pct > ma_spread_threshold * 1.5:
                analysis['ma_spread'] = "多头大幅发散"
            elif spread_pct < -ma_spread_threshold * 1.5:
                analysis['ma_spread'] = "空头大幅发散"
            elif spread_pct > ma_spread_threshold:
                analysis['ma_spread'] = "多头发散"
            elif spread_pct < -ma_spread_threshold:
                analysis['ma_spread'] = "空头发散"
            else:
                analysis['ma_spread'] = "正常"
        else:
            analysis['ma_spread'] = "数据不足"

        # ==================== 5. 量能分析 ====================
        if 'Volume_Ratio' in df.columns and pd.notna(latest['Volume_Ratio']):
            ratio = latest['Volume_Ratio']
            if ratio > vol_high:
                analysis['volume_status'] = f"放量（量比{ratio:.2f}）"
            elif ratio < vol_low:
                analysis['volume_status'] = f"缩量（量比{ratio:.2f}）"
            else:
                analysis['volume_status'] = f"量能正常（量比{ratio:.2f}）"

        # ==================== 6. 量价配合关系 ====================
        if 'Volume_Ratio' in df.columns and pd.notna(latest['Volume_Ratio']):
            ratio = latest['Volume_Ratio']
            # 修正2：使用周期化阈值 price_move_threshold 替代固定值 0.3
            if ratio > vol_high and change_pct > price_move_threshold:
                analysis['volume_price'] = "放量上涨（健康）"
            elif ratio > vol_high and change_pct < -price_move_threshold:
                analysis['volume_price'] = "放量下跌（警惕）"
            elif ratio < vol_low and change_pct > price_move_threshold:
                analysis['volume_price'] = "缩量上涨（背离）"
            elif ratio < vol_low and change_pct < -price_move_threshold:
                analysis['volume_price'] = "缩量下跌（阴跌/惜售）"
            else:
                analysis['volume_price'] = "量价常态"

        # ==================== 7. 换手率分析 ====================
        if 'turnover_rate' in df.columns and pd.notna(latest['turnover_rate']):
            turnover = latest['turnover_rate']
            avg_turnover = df['turnover_rate'].tail(20).mean() if len(df) >= 20 else turnover
            if turnover > avg_turnover * 1.5:
                analysis['turnover_status'] = f"高换手（{turnover:.2f}%，高于均值）"
            elif turnover < avg_turnover * 0.5:
                analysis['turnover_status'] = f"低换手（{turnover:.2f}%，低于均值）"
            else:
                analysis['turnover_status'] = f"换手正常（{turnover:.2f}%）"

        return analysis

    def get_multi_period_data(self, index_name: str, periods: List[str] = None) -> Dict[str, pd.DataFrame]:
        if periods is None:
            periods = ["5min", "15min", "30min", "日线", "周线"]

        # 定义各周期的 MA 参数
        ma_periods_map = {
            "5min": [5, 10, 20],
            "15min": [5, 10, 20],
            "30min": [5, 10, 20, 60],
            "60min": [5, 10, 20, 60],
            "日线": [5, 10, 20, 60, 120],
            "周线": [5, 10, 20, 60],
        }

        # 8/14: 各周期 MACD 参数（A股专属，与 trade_data_fetcher 完全一致）
        macd_params_map = {
            "5min": (7, 14, 5),
            "15min": (10, 20, 7),
            "30min": (10, 20, 7),
            "60min": (8, 20, 6),
            "日线": (10, 20, 7),
            "周线": (10, 20, 7),
        }
        boll_params_map = {
            "5min": (10, 1.9),
            "15min": (20, 2.0),
            "30min": (20, 2.0),
            "60min": (20, 2.0),
            "日线": (20, 2.0),
            "周线": (50, 2.1),
        }

        result = {}
        for period in periods:
            df = self.get_kline_data(index_name, period)
            if df is not None:
                ma_periods = ma_periods_map.get(period, [5, 10, 20, 60])
                df = self.calculate_ma(df, periods=ma_periods)
                df = self.calculate_volume_ma(df)
                df = self.get_volume_ratio(df)
                # 8/14: 复用 trade 侧 MACD + BOLL 指标（按周期传 A股参数）
                macd_p = macd_params_map.get(period, (7, 14, 5))
                boll_p = boll_params_map.get(period, (20, 2.0))
                try:
                    df = calculate_macd_boll(
                        df,
                        macd_fast=macd_p[0], macd_slow=macd_p[1], macd_signal=macd_p[2],
                        boll_period=int(boll_p[0]), boll_std=boll_p[1],
                    )
                except Exception as e:
                    logging.warning(f"MACD/BOLL 计算失败 ({period}): {e}")
                result[period] = df
        return result

    def _format_recent_candles(self, df: pd.DataFrame, period: str) -> str:
        n_map = {"5min": 48, "15min": 32, "30min": 20, "60min": 12, "日线": 15, "周线": 10}
        n = n_map.get(period, 10)
        if df.empty or len(df) < n:
            n = len(df)
        recent = df.tail(n).copy()

        lines = [
            f"\n**最近 {n} 根 {period} K 线**",
            "| 时间 | 开盘 | 最高 | 最低 | 收盘 | 成交量 | 涨跌幅 | 换手率 |",
            "|------|------|------|------|------|--------|--------|--------|"
        ]

        for _, row in recent.iterrows():
            if 'datetime' in row and pd.notna(row['datetime']):
                if period in ["5min", "15min", "30min", "60min"]:
                    time_str = row['datetime'].strftime('%H:%M')
                else:
                    time_str = row['datetime'].strftime('%m-%d')
            else:
                time_str = str(row.get('date', ''))

            change_pct = (row['close'] - row['open']) / row['open'] * 100 if row['open'] != 0 else 0
            volume_str = f"{row['volume'] / 1e6:.1f}M" if row['volume'] > 1e6 else f"{row['volume']:.0f}"

            # 换手率处理
            if 'turnover_rate' in row and pd.notna(row['turnover_rate']):
                turnover_str = f"{row['turnover_rate']:.2f}%"
            else:
                turnover_str = "-"

            lines.append(
                f"| {time_str} | {row['open']:.2f} | {row['high']:.2f} | {row['low']:.2f} | "
                f"{row['close']:.2f} | {volume_str} | {change_pct:+.2f}% | {turnover_str} |"
            )

        return "\n".join(lines)

    def generate_ai_prompt(self, index_name: str = "中证1000", periods: List[str] = None) -> str:
        data_dict = self.get_multi_period_data(index_name, periods)
        if not data_dict:
            return "数据获取失败"

        prompt_parts = []

        # ========== 插入亚洲指数完整 5 分钟 K 线表格 + 最新涨跌幅 ==========
        asian_snapshot = self.get_asian_indices_5min_bars()
        if asian_snapshot:
            current_hour = datetime.now().hour
            if 8 <= current_hour < 12:
                title = "日韩早盘 5 分钟走势参考"
            elif 12 <= current_hour < 14:
                title = "日韩午盘 5 分钟走势参考"
            else:
                title = "日韩盘面 5 分钟走势参考"

            prompt_parts.append(f"## {title}")

            indices_data = asian_snapshot.get("indices", {})
            for key, name in [("nikkei225", "日经225"), ("kospi", "韩国KOSPI")]:
                idx_info = indices_data.get(key, {})
                if not idx_info:
                    prompt_parts.append(f"### {name}\n数据获取失败\n")
                    continue

                bars = idx_info.get("5min_bars", [])
                prev_close = idx_info.get("prev_close", 0)
                if not bars:
                    prompt_parts.append(f"### {name}\n暂无5分钟K线数据\n")
                    continue

                # 提取最新数据用于快照描述
                last_bar = bars[-1]
                latest_price = last_bar['close']
                latest_change_pct = last_bar['change_pct_from_prev_close']
                change_symbol = '+' if latest_change_pct >= 0 else ''
                direction = "上涨" if latest_change_pct > 0 else "下跌" if latest_change_pct < 0 else "持平"

                prompt_parts.append(f"**{name}** 前收盘 {prev_close:.2f}，")
                prompt_parts.append(f"最新 {latest_price:.2f}，")
                prompt_parts.append(f"较前收 {direction} {change_symbol}{latest_change_pct:.2f}%\n")

                prompt_parts.append(f"**当日全部 {len(bars)} 根 5 分钟 K 线**")
                prompt_parts.append("| 时间 | 开盘 | 最高 | 最低 | 收盘 | 成交量 | 涨跌幅(较前收) |")
                prompt_parts.append("|------|------|------|------|------|--------|----------------|")

                for bar in bars:
                    # 提取 HH:MM 部分
                    time_str = bar['time'][-8:-3]  # 假设格式为 "YYYY-MM-DD HH:MM:SS"
                    o = bar['open']
                    h = bar['high']
                    l = bar['low']
                    c = bar['close']
                    vol = bar.get('volume', 0)
                    chg_pct = bar['change_pct_from_prev_close']

                    # 格式化成交量（可选）
                    if vol >= 1_000_000:
                        vol_str = f"{vol / 1_000_000:.1f}M"
                    elif vol >= 1_000:
                        vol_str = f"{vol / 1_000:.1f}K"
                    else:
                        vol_str = str(vol)

                    # 涨跌幅带符号显示
                    chg_str = f"{'+' if chg_pct >= 0 else ''}{chg_pct:.2f}%"

                    prompt_parts.append(
                        f"| {time_str} | {o:.2f} | {h:.2f} | {l:.2f} | {c:.2f} | {vol_str} | {chg_str} |"
                    )

                prompt_parts.append("")  # 空行分隔不同指数

            prompt_parts.append("")
        # =====================================

        prompt_parts.append(f"# {index_name} 多周期技术面分析数据")
        prompt_parts.append(f"数据更新时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

        for period, df in data_dict.items():
            if df.empty:
                continue
            latest = df.iloc[-1]
            analysis = self.analyze_trend(df)
            prompt_parts.append(f"## {period}周期")
            time_key = 'datetime' if 'datetime' in df.columns else 'date'
            time_val = latest[time_key]
            if hasattr(time_val, 'strftime'):
                time_str = time_val.strftime('%Y-%m-%d %H:%M') if period in ["5min","15min","30min","60min"] else time_val.strftime('%Y-%m-%d')
            else:
                time_str = str(time_val)
            prompt_parts.append(f"- 最新K线时间：{time_str}")
            prompt_parts.append(f"- 开:{latest['open']:.2f} 高:{latest['high']:.2f} 低:{latest['low']:.2f} 收:{latest['close']:.2f}")
            turnover_info = analysis.get('turnover_status', 'N/A')
            prompt_parts.append(
                f"- 涨跌幅：{analysis.get('change_pct', 'N/A')} 量比：{analysis.get('volume_status', 'N/A')} 换手率：{turnover_info}")
            ma_info = [f"{col}={latest[col]:.2f}" for col in df.columns if re.match(r'^MA\d+$', col) and pd.notna(latest[col])]
            if ma_info:
                prompt_parts.append(f"- 均线：{', '.join(ma_info)}")
            prompt_parts.append(f"- 均线排列：{analysis.get('ma_arrangement', 'N/A')}")

            # ---------- 新增：补充字段 ----------
            # 1. 均线位置（如 MA5:站上, MA10:跌破）
            ma_position_items = []
            for col in df.columns:
                if re.match(r'^MA\d+$', col) and f'{col}_position' in analysis:
                    ma_position_items.append(f"{col}:{analysis[f'{col}_position']}")
            if ma_position_items:
                prompt_parts.append(f"- 均线位置：{', '.join(ma_position_items)}")

            # 2. 均线发散状态
            prompt_parts.append(f"- 均线发散：{analysis.get('ma_spread', 'N/A')}")

            # 3. 量价配合关系
            if 'volume_price' in analysis:
                prompt_parts.append(f"- 量价配合：{analysis['volume_price']}")

            # 4. 跳空缺口（若有）
            if 'gap' in analysis:
                prompt_parts.append(f"- 跳空缺口：{analysis['gap']}")
            # --------------------------------

            # 8/14: MACD / 布林带（复用 trade 侧 A股参数指标，供点位依据参考）
            try:
                mb = analyze_macd_boll(df, period=period)
                if mb.get('macd'):
                    prompt_parts.append(f"- {mb['macd']}")
                if mb.get('boll'):
                    prompt_parts.append(f"- {mb['boll']}")
            except Exception:
                pass

            prompt_parts.append(self._format_recent_candles(df, period))
            prompt_parts.append("")
        return "\n".join(prompt_parts)

    def get_daily_snapshot(self, index_name: str = "中证1000") -> dict:
        df = self.get_kline_data(index_name, "日线")
        if df is None or df.empty:
            return {}
        df = self.calculate_ma(df)
        df = self.calculate_volume_ma(df)
        df = self.get_volume_ratio(df)
        latest = df.iloc[-1]
        analysis = self.analyze_trend(df)
        return {
            'latest_price': f"{latest['close']:.2f}",
            'change_pct': analysis.get('change_pct', 'N/A'),
            'ma5': f"{latest['MA5']:.2f}" if 'MA5' in latest and pd.notna(latest['MA5']) else 'N/A',
            'ma20': f"{latest['MA20']:.2f}" if 'MA20' in latest and pd.notna(latest['MA20']) else 'N/A',
            'volume_ratio': analysis.get('volume_status', 'N/A'),
            'date': latest['date']
        }

    # ==================== 亚洲指数相关（保留原有实现）====================
    def get_asian_indices_5min_bars(self) -> Dict[str, dict]:
        result = {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "indices": {}}
        # Yahoo Finance 需要代理，但 eastmoney 不需要
        proxy_url = os.environ.get("HTTPS_PROXY", os.environ.get("HTTP_PROXY"))
        if proxy_url:
            proxies = {"http": proxy_url, "https": proxy_url}
        else:
            proxies = None
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Referer": "https://finance.yahoo.com/"}
        index_map = {"nikkei225": "^N225", "kospi": "^KS11"}
        for key, symbol in index_map.items():
            try:
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=5m&range=1d"
                resp = requests.get(url, headers=headers, proxies=proxies, timeout=10)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                chart_result = data.get("chart", {}).get("result", [])
                if not chart_result:
                    continue
                result_data = chart_result[0]
                meta = result_data.get("meta", {})
                prev_close = meta.get("previousClose")
                if prev_close is None or prev_close == 0:
                    continue
                timestamps = result_data.get("timestamp", [])
                indicators = result_data.get("indicators", {})
                quote = indicators.get("quote", [{}])[0]
                opens = quote.get("open", [])
                highs = quote.get("high", [])
                lows = quote.get("low", [])
                closes = quote.get("close", [])
                volumes = quote.get("volume", [])
                bars = []
                for i, ts in enumerate(timestamps):
                    if i >= len(opens) or i >= len(highs) or i >= len(lows) or i >= len(closes):
                        break
                    o, h, l, c = opens[i], highs[i], lows[i], closes[i]
                    if None in (o, h, l, c):
                        continue
                    change_pct = (c - prev_close) / prev_close * 100
                    bars.append({
                        "time": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
                        "open": round(o, 2), "high": round(h, 2), "low": round(l, 2), "close": round(c, 2),
                        "volume": int(volumes[i]) if i < len(volumes) and volumes[i] else 0,
                        "change_pct_from_prev_close": round(change_pct, 2)
                    })
                result["indices"][key] = {"symbol": symbol, "prev_close": round(prev_close, 2), "5min_bars": bars, "bar_count": len(bars)}
            except Exception as e:
                logging.warning(f"获取 {key} 5min 数据失败: {e}")
        return result

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


if __name__ == "__main__":
    with IndexDataFetcher() as fetcher:
        prompt = fetcher.generate_ai_prompt()
        print(prompt)