import os
import sys
import re
import csv
import time
import pickle
import requests
import logging
from dotenv import load_dotenv

load_dotenv()

# 启用东方财富反爬补丁 (ENABLE_EASTMONEY_PATCH)
# 来源: https://github.com/ZhuLinsen/daily_stock_analysis/blob/main/src/patches/eastmoney_patch.py
# 必须在 import efinance 之前启用
# 注意：保留原有 no_proxy（localhost 等），并追加东财 + 腾讯备用源域名
os.environ['no_proxy'] = ','.join(filter(None, [
    os.environ.get('no_proxy', ''),
    'push2his.eastmoney.com,push2.eastmoney.com,fund.eastmoney.com,anonflow2.eastmoney.com',
    'ifzq.gtimg.cn,qt.gtimg.cn',
]))

# 兼容不同运行路径：eastmoney_patch.py 与 trade_data_fetcher.py 同目录
_current_dir = os.path.dirname(os.path.abspath(__file__))
if _current_dir not in sys.path:
    sys.path.insert(0, _current_dir)

from eastmoney_patch import enable_eastmoney_patch
enable_eastmoney_patch()

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, Optional, List
import time
import requests
import re
import logging
import csv

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


# ==================== K线磁盘缓存（跨进程复用，避免每次提问/决策重复全量拉取） ====================
# namespace 区分数据源：trade=本文件（东财/腾讯）、report=baostock_daily（东财），互不覆盖
_KLINE_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_cache", "kline")


def _disk_cache_path(index_key: str, frequency: str, namespace: str) -> str:
    safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", f"{index_key}_{frequency}")
    return os.path.join(_KLINE_CACHE_DIR, namespace, f"{safe}.pkl")


def load_disk_cache(index_key: str, frequency: str, ttl_seconds: float,
                    namespace: str = "trade") -> Optional[pd.DataFrame]:
    """读取未过期的磁盘K线缓存；不存在/过期返回 None。"""
    path = _disk_cache_path(index_key, frequency, namespace)
    try:
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            saved_ts, df = pickle.load(f)
        if time.time() - saved_ts < ttl_seconds:
            return df
    except Exception as e:
        print(f"[kline-cache] 读取缓存失败 {path}: {e}")
    return None


def save_disk_cache(index_key: str, frequency: str, df: pd.DataFrame,
                    namespace: str = "trade") -> None:
    """把K线DataFrame写入磁盘缓存（含抓取时间戳）。失败不阻断主流程。"""
    try:
        path = _disk_cache_path(index_key, frequency, namespace)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump((time.time(), df), f, protocol=4)
    except Exception as e:
        print(f"[kline-cache] 保存缓存失败 {path}: {e}")


def merge_klines(cached: Optional[pd.DataFrame], fresh: pd.DataFrame) -> pd.DataFrame:
    """把磁盘缓存与最新抓取的数据按 datetime 拼接（时间拼接防呆）。

    规则：
    - 统一以 datetime 列（Timestamps）为准，缓存与新数据同源，格式一致
    - 重叠时间行以最新抓取为准（drop_duplicates keep='last'）
    - 结果按时间升序；只保留最后 max(len(fresh)*2, 600) 根，防止无限膨胀
    - 任一步异常都回退为直接使用新数据，绝不阻塞主流程
    """
    if fresh is None or fresh.empty:
        return cached.copy() if cached is not None and not cached.empty else fresh
    if cached is None or cached.empty or "datetime" not in cached.columns:
        return fresh.copy()
    try:
        cols = list(fresh.columns)
        cached_cols = [c for c in cols if c in cached.columns]
        merged = pd.concat([cached[cached_cols], fresh[cols]], ignore_index=True)
        merged = merged.drop_duplicates(subset=["datetime"], keep="last")
        merged = merged.sort_values("datetime").reset_index(drop=True)
        cap = max(len(fresh) * 2, 600)
        if len(merged) > cap:
            merged = merged.tail(cap).reset_index(drop=True)
        return merged
    except Exception as e:
        print(f"[kline-cache] 时间拼接失败，直接使用新数据: {e}")
        return fresh.copy()


def clear_disk_cache() -> None:
    """清空全部K线磁盘缓存（换月/手工刷新时强制重新拉取）"""
    try:
        import shutil
        if os.path.isdir(_KLINE_CACHE_DIR):
            shutil.rmtree(_KLINE_CACHE_DIR, ignore_errors=True)
    except Exception as e:
        print(f"[kline-cache] 清空失败: {e}")


# ==================== MACD + 布林带（模块级实现，交易侧/报告侧共用） ====================
def calc_macd_boll(df: pd.DataFrame, macd_fast: int = 7, macd_slow: int = 14,
                   macd_signal: int = 5, boll_period: int = 20, boll_std: float = 2.0) -> pd.DataFrame:
    """
    计算 MACD + 布林带（8/14 新增）
    默认参数：MACD(7,14,5)=A股5min短线、BOLL(20,2)=黄金标准。
    各周期实际参数由调用方按周期传入：
      MACD: 5min=(7,14,5) 15/30min/日线/周线=(10,20,7) 60min=(8,20,6)
      BOLL: 5min=(10,1.9) 15/30/60min/日线=(20,2) 周线=(50,2.1)
      （Bollinger 本人建议：周期与标准差反向调整）
    """
    df = df.copy()
    close = df['close']
    # MACD
    ema_fast = close.ewm(span=macd_fast, adjust=False).mean()
    ema_slow = close.ewm(span=macd_slow, adjust=False).mean()
    df['DIF'] = ema_fast - ema_slow
    df['DEA'] = df['DIF'].ewm(span=macd_signal, adjust=False).mean()
    df['MACD_HIST'] = 2.0 * (df['DIF'] - df['DEA'])  # 柱状线（国内习惯 2×）
    # 布林带
    mid = close.rolling(window=boll_period).mean()
    std = close.rolling(window=boll_period).std()
    df['BOLL_MID'] = mid
    df['BOLL_UP'] = mid + boll_std * std
    df['BOLL_LOW'] = mid - boll_std * std
    df['BOLL_WIDTH'] = (df['BOLL_UP'] - df['BOLL_LOW']) / mid  # 带宽（收窄检测用）
    return df


def analyze_macd_boll_state(df: pd.DataFrame, period: str = "5min") -> Dict[str, str]:
    """
    MACD/布林带状态分析（8/14 新增）——为 AI 提供真实计算的指标状态
    与 prompt 中"MACD 金叉/死叉 +0.1、布林收窄 +0.1"评分规则对应
    MACD 参数由 calc_macd_boll 时按周期传入（A股专属参数表）
    """
    result = {}
    if df is None or len(df) < 25:
        return result

    # ==================== MACD 状态 ====================
    try:
        dif_cur = float(df['DIF'].iloc[-1])
        dea_cur = float(df['DEA'].iloc[-1])
        hist_cur = float(df['MACD_HIST'].iloc[-1])
        hist_prev = float(df['MACD_HIST'].iloc[-2])
        dif_prev = float(df['DIF'].iloc[-2])
        dea_prev = float(df['DEA'].iloc[-2])

        # 粘合保护（8/14 新增）：DIF 与 DEA 差小于 DIF 幅度的 0.5% 时判"粘合"，
        # 避免微噪声导致金叉/死叉频繁翻转（真实行情中 DIF≈DEA 是常态）
        dif_scale = max(abs(dif_cur), abs(dea_cur), 1e-9)
        if abs(dif_cur - dea_cur) < dif_scale * 0.005:
            macd_state = "MACD粘合（DIF≈DEA，方向待选）"
        elif dif_prev <= dea_prev and dif_cur > dea_cur:
            macd_state = "MACD金叉"
        elif dif_prev >= dea_prev and dif_cur < dea_cur:
            macd_state = "MACD死叉"
        elif dif_cur > dea_cur:
            macd_state = "MACD多头（DIF>DEA）"
        elif dif_cur < dea_cur:
            macd_state = "MACD空头（DIF<DEA）"
        else:
            macd_state = "MACD粘合"

        # 柱状线扩张/收缩（趋势增强/减弱）
        if abs(hist_cur) > abs(hist_prev) and hist_cur * hist_prev >= 0:
            hist_state = "柱状线扩张（动能增强）"
        elif abs(hist_cur) < abs(hist_prev) and hist_cur * hist_prev >= 0:
            hist_state = "柱状线收缩（动能减弱）"
        else:
            hist_state = "柱状线换向"

        result['macd'] = (f"{macd_state}，{hist_state} "
                          f"（DIF={dif_cur:.2f}, DEA={dea_cur:.2f}, 柱={hist_cur:.2f}, 前柱={hist_prev:.2f}）")
    except Exception:
        result['macd'] = "MACD数据不足"

    # ==================== 布林带状态 ====================
    try:
        cur_close = float(df['close'].iloc[-1])
        boll_up = float(df['BOLL_UP'].iloc[-1])
        boll_low = float(df['BOLL_LOW'].iloc[-1])
        boll_mid = float(df['BOLL_MID'].iloc[-1])
        width_cur = float(df['BOLL_WIDTH'].iloc[-1])
        width_20 = list(df['BOLL_WIDTH'].iloc[-21:-1])
        width_20 = [w for w in width_20 if pd.notna(w)]

        # 位置
        if cur_close > boll_up:
            pos_state = "突破上轨（超买）"
        elif cur_close < boll_low:
            pos_state = "跌破下轨（超卖）"
        elif cur_close > boll_mid:
            pos_state = "中轨上方"
        else:
            pos_state = "中轨下方"

        # 带宽收缩（变盘前兆，prompt 中"布林带宽度收缩至近20根K线最低"对应）
        if width_20 and width_cur <= min(width_20) * 1.05:
            width_state = "带宽极度收窄（变盘前兆）"
        elif width_20 and width_cur <= min(width_20) * 1.2:
            width_state = "带宽收窄中"
        elif width_20 and width_cur >= max(width_20) * 0.95:
            width_state = "带宽扩张（波动加大）"
        else:
            width_state = "带宽正常"

        result['boll'] = (f"布林带：{pos_state}，{width_state} "
                          f"（上轨{boll_up:.2f}, 中轨{boll_mid:.2f}, 下轨{boll_low:.2f}）")
    except Exception:
        result['boll'] = "布林带数据不足"

    return result


class IndexDataFetcher:
    """基于 efinance 的指数多周期数据获取器，直接获取真实指数K线"""

    # 修复 M4: 启用 TTL 缓存 —— 分钟线盘中每 5 分钟变一根，TTL 须短于决策间隔
    _CACHE_TTL_SECONDS_INTRADAY = 60    # 5min/15min/30min/60min
    _CACHE_TTL_SECONDS_SLOW = 300       # 日线/周线
    _CACHE_MAX_ENTRIES = 200

    # 指数名称映射（efinance 可以直接使用中文名称）
    INDEX_NAME_MAP = {
        "中证1000": "中证1000",
        "上证指数": "上证指数",
    }

    # 备用数据源（腾讯）代码映射：中文名 -> 腾讯 symbol
    _TENCENT_INDEX_MAP = {"中证1000": "sh000852", "上证指数": "sh000001"}
    try:
        from akshare_multi_period import INDEX_SYMBOL_MAP as _AK_INDEX_MAP
        _TENCENT_INDEX_MAP = dict(_AK_INDEX_MAP)
    except ImportError:
        pass

    # 腾讯周期映射：(接口周期, 单根分钟数)；分钟类走 mkline，日/周走 fqkline
    _TENCENT_PERIOD_MAP = {
        "5min": ("m5", 5),
        "15min": ("m15", 15),
        "30min": ("m30", 30),
        "60min": ("m60", 60),
        "日线": ("day", None),
        "周线": ("week", None),
    }

    # 东财限流保护：非日线周期一律走腾讯；日线周期每日最多尝试一次东财
    # （东财日线价值最高：含历史换手率 f61；其余周期腾讯已足够）
    _EFINANCE_LAST_TRY_DATE = None

    # 默认周期配置（类常量，用作未传参时的默认值）
    _DEFAULT_PERIOD_CONFIG = {
        "5min": {"klt": 5, "days_back": 5},
        "15min": {"klt": 15, "days_back": 7},
        "30min": {"klt": 30, "days_back": 15},
        "60min": {"klt": 60, "days_back": 30},
        "日线": {"klt": 101, "days_back": 180},
        "周线": {"klt": 102, "days_back": 365 * 2},
    }

    def __init__(self, period_config: Optional[dict] = None):
        """
        初始化数据获取器

        Parameters
        ----------
        period_config : dict, optional
            自定义周期配置，格式需与 _DEFAULT_PERIOD_CONFIG 一致。
            若未提供，则使用默认配置。
        """
        if not EFINANCE_AVAILABLE:
            raise ImportError("efinance 未安装，无法使用本类")

        # 若未指定则拷贝默认配置（避免直接引用类变量被意外修改）
        self.period_config = period_config if period_config is not None else self._DEFAULT_PERIOD_CONFIG.copy()
        self._cache = {}  # 可选：缓存已获取的数据

    def _get_kline_efinance(self, index_name: str, frequency: str) -> Optional[pd.DataFrame]:
        """使用 efinance 获取指定指数的K线数据"""
        if index_name not in self.INDEX_NAME_MAP:
            print(f"未配置的指数名称: {index_name}")
            return None

        config = self.period_config.get(frequency)  # 改用实例属性
        if not config:
            print(f"不支持的周期: {frequency}")
            return None

        # 东财限流保护：非日线周期一律走腾讯；日线每日最多尝试一次东财
        today = datetime.now().strftime('%Y%m%d')
        if frequency != "日线" or IndexDataFetcher._EFINANCE_LAST_TRY_DATE == today:
            print(f"[efinance] 限流保护：{index_name} {frequency} 直接使用腾讯备用源")
            return self._get_kline_tencent(index_name, frequency)
        IndexDataFetcher._EFINANCE_LAST_TRY_DATE = today

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

            # 转换数据类型（合并去重，避免重复 to_numeric）
            numeric_cols = ['open', 'high', 'low', 'close', 'volume', 'amount', 'turnover_rate']
            for col in numeric_cols:
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

    def _get_kline_tencent(self, index_name: str, frequency: str) -> Optional[pd.DataFrame]:
        """备用数据源：腾讯财经 (ifzq.gtimg.cn)

        push2his.eastmoney.com 服务端限流/节点不可达时，efinance 必然失败。
        腾讯 mkline/fqkline 接口无访问限制，可完整接管 5/15/30/60 分钟与日/周线。
        """
        symbol = self._TENCENT_INDEX_MAP.get(index_name)
        spec = self._TENCENT_PERIOD_MAP.get(frequency)
        if not symbol or not spec:
            print(f"[tencent] 未配置 {index_name} {frequency} 的备用源映射")
            return None

        config = self.period_config.get(frequency)
        days_back = config["days_back"] if config else 30
        tencent_period, minutes = spec

        try:
            if minutes:  # 分钟线：mkline 单次上限 320 根，覆盖各周期 days_back 需求
                count = 320
                url = (f"https://ifzq.gtimg.cn/appstock/app/kline/mkline"
                       f"?param={symbol},{tencent_period},,{count}")
            else:  # 日线/周线：fqkline
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

            # 日线补充换手率：当日值取自 fqkline 响应内 qt 快照字段38，历史值取自本地累积文件
            if frequency == "日线":
                df = self._attach_daily_turnover(symbol, df, resp)

            print(f"[tencent] 获取 {index_name} {frequency} 备用数据源成功，共 {len(df)} 条")
            return df
        except Exception as e:
            print(f"[tencent] 获取 {index_name} {frequency} 失败: {e}")
            return None

    def _attach_daily_turnover(self, symbol: str, df: pd.DataFrame, resp) -> pd.DataFrame:
        """给日线 DataFrame 附加换手率列

        腾讯 K线接口不返回换手率，但 fqkline 响应内的 qt 快照节点携带当日指数换手率
        （字段38，聚合口径，量纲与东财 f61 一致，如 2.84 = 2.84%）。
        历史换手率无免费源提供，采用本地累积：每日把当日值写入
        data_cache/index_turnover_{symbol}.csv，逐日沉淀出历史序列。
        """
        df["turnover_rate"] = np.nan

        # 1) 当日值：qt 快照字段38（与 K线同一次响应，零额外请求）
        today_turnover = None
        try:
            qt = resp.json().get("data", {}).get(symbol, {}).get("qt", {})
            qt_fields = qt.get(symbol) if isinstance(qt, dict) else None
            if isinstance(qt_fields, (list, tuple)) and len(qt_fields) > 38:
                v = str(qt_fields[38]).strip()
                if v and v != "0":
                    today_turnover = float(v)
        except Exception:
            pass

        # 2) 本地累积文件（date -> turnover）
        store_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_cache")
        store_path = os.path.join(store_dir, f"index_turnover_{symbol}.csv")
        history = {}
        try:
            if os.path.exists(store_path):
                with open(store_path, "r", encoding="utf-8") as f:
                    for row in csv.reader(f):
                        if len(row) == 2 and row[0] != "date":
                            try:
                                history[row[0]] = float(row[1])
                            except ValueError:
                                pass
        except Exception as e:
            print(f"[tencent] 读取换手率累积文件失败: {e}")

        # 3) 写入当日值并落盘
        today_str = datetime.now().strftime("%Y-%m-%d")
        if today_turnover is not None:
            history[today_str] = today_turnover
            try:
                os.makedirs(store_dir, exist_ok=True)
                with open(store_path, "w", encoding="utf-8", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["date", "turnover"])
                    for d, t in sorted(history.items()):
                        writer.writerow([d, t])
            except Exception as e:
                print(f"[tencent] 写入换手率累积文件失败: {e}")

        # 4) 合并进 DataFrame（按日期匹配）
        matched = 0
        for i in range(len(df)):
            d = df.loc[i, "date"]
            if d in history:
                df.loc[i, "turnover_rate"] = history[d]
                matched += 1
        if matched:
            print(f"[tencent] 换手率已合并 {matched} 条（当日 {today_turnover if today_turnover is not None else 'N/A'}%）")
        return df

    def get_kline_data(self, index_name: str, frequency: str) -> Optional[pd.DataFrame]:
        """统一入口，获取指定周期的K线数据

        修复 M4: 启用 TTL 缓存（原 _cache 定义后从未使用，每 AI cycle 重复 10+ 次网络请求）。
        缓存 key 含日期，跨日自动失效；返回副本防止调用方就地修改污染缓存。
        8/14: 增加跨进程磁盘缓存（data_cache/kline/），autotrade_fix 与报告侧共用；
              缓存过期时用「缓存历史 + 最新数据」拼接，保留 MACD/MA 等指标的预热历史。
        """
        now_ts = time.time()
        cache_key = (index_name, frequency, datetime.now().strftime('%Y%m%d'))
        ttl = (self._CACHE_TTL_SECONDS_INTRADAY
               if frequency in ("5min", "15min", "30min", "60min")
               else self._CACHE_TTL_SECONDS_SLOW)

        cached = self._cache.get(cache_key)
        if cached and now_ts - cached[0] < ttl:
            return cached[1].copy()

        # 磁盘缓存（跨进程）：未过期直接复用，不再发网络请求
        disk_df = load_disk_cache(index_name, frequency, ttl_seconds=ttl, namespace="trade")
        if disk_df is not None and len(disk_df) > 0:
            self._cache[cache_key] = (now_ts, disk_df)
            return disk_df.copy()

        df = self._get_kline_efinance(index_name, frequency)
        if df is not None:
            # 拼接缓存历史，保留指标预热数据；随后落盘供其他进程复用
            df = merge_klines(disk_df, df)
            save_disk_cache(index_name, frequency, df, namespace="trade")
            self._cache[cache_key] = (now_ts, df)
            # 简单防膨胀：超过上限时淘汰最旧条目
            if len(self._cache) > self._CACHE_MAX_ENTRIES:
                oldest_key = min(self._cache, key=lambda k: self._cache[k][0])
                del self._cache[oldest_key]
            # 未命中路径同样返回副本，防止调用方就地修改污染缓存
            return df.copy()
        return None

    def clear_cache(self) -> None:
        """清空缓存（供换月/手工刷新等场景强制重新拉取）"""
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

    def calculate_macd_boll(self, df: pd.DataFrame, macd_fast: int = 7, macd_slow: int = 14,
                            macd_signal: int = 5, boll_period: int = 20, boll_std: float = 2.0) -> pd.DataFrame:
        """计算 MACD + 布林带（委托模块级函数，供本类与报告侧复用）"""
        return calc_macd_boll(df, macd_fast, macd_slow, macd_signal, boll_period, boll_std)

    def analyze_macd_boll(self, df: pd.DataFrame, period: str = "5min") -> Dict[str, str]:
        """MACD/布林带状态分析（委托模块级函数，供本类与报告侧复用）"""
        return analyze_macd_boll_state(df, period)

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
        # 8/14 修复: 用正则 MA\d+ 精确匹配均线列，避免误匹配 MACD_HIST/MACD_* 等列
        # （MACD 列加入后，startswith('MA') 会把 MACD_HIST 当作均线 → int('CD_HIST') 崩溃 → 整个技术数据刷新失败）
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

        # 8/14: 各周期 MACD 参数（A股专属，社区共识 + 理论推导）
        # 默认 (12,26,9) 基于美股每周6个交易日设计，A股每周5个交易日必须缩短
        #   5min: (7,14,5)  A股 1-5min K线高频经验参数
        #   15min: (10,20,7) A股标准短线推荐
        #   30min: (10,20,7) 同短线
        #   60min: (8,20,6)  时/日/周时间框架理论推导
        #   日线:  (10,20,7) A股最广泛推荐
        #   周线:  (10,20,7) A股推荐
        macd_params_map = {
            "5min": (7, 14, 5),
            "15min": (10, 20, 7),
            "30min": (10, 20, 7),
            "60min": (8, 20, 6),
            "日线": (10, 20, 7),
            "周线": (10, 20, 7),
        }

        # 8/14: 各周期布林带参数（Bollinger 本人建议：周期与 σ 反向调整）
        #   短周期(5min) → (10, 1.9)：更灵敏，σ 同步减小防通道过窄假信号
        #   15/30/60min/日线 → (20, 2)：黄金标准，多数市场适配
        #   周线 → (50, 2.1)：长周期趋势，σ 增大防趋势中被洗出
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
                # 8/14: 按周期 MACD + BOLL 指标列
                macd_p = macd_params_map.get(period, (7, 14, 5))
                boll_p = boll_params_map.get(period, (20, 2.0))
                df = self.calculate_macd_boll(
                    df,
                    macd_fast=macd_p[0], macd_slow=macd_p[1], macd_signal=macd_p[2],
                    boll_period=int(boll_p[0]), boll_std=boll_p[1],
                )
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

            # ========== 8/14 新增：MACD + 布林带状态（真实计算，AI 直接读取） ==========
            mb = self.analyze_macd_boll(df, period)
            if mb.get('macd'):
                prompt_parts.append(f"- {mb['macd']}")
            if mb.get('boll'):
                prompt_parts.append(f"- {mb['boll']}")
            # =======================================================================

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
        """使用 yfinance 获取亚洲指数（日经225/KOSPI）5分钟K线数据

        相比直接请求 Yahoo Finance API，yfinance 内部处理了：
        - Cookie/Crumb 认证流程
        - Session 管理与复用
        - 请求重试与退避
        - 时区处理
        - 更可靠的成交量数据
        """
        result = {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "indices": {}}

        if not YFINANCE_AVAILABLE:
            logging.warning("yfinance 未安装，无法获取亚洲指数数据")
            return result

        index_map = {"nikkei225": "^N225", "kospi": "^KS11"}
        for key, symbol in index_map.items():
            try:
                ticker = yf.Ticker(symbol)

                # 获取前收盘价
                try:
                    info = ticker.info
                    prev_close = tinfo.get('previousClose')
                except Exception:
                    prev_close = None

                if prev_close is None or prev_close == 0:
                    # 降级方案：从日线历史获取昨日收盘
                    try:
                        daily = ticker.history(period='5d', interval='1d')
                        if len(daily) >= 2:
                            prev_close = daily['Close'].iloc[-2]
                        elif not daily.empty:
                            prev_close = daily['Close'].iloc[-1]
                        else:
                            continue
                    except Exception:
                        continue

                # 获取当日5分钟数据
                df = ticker.history(period='1d', interval='5m')
                if df.empty:
                    continue

                bars = []
                for idx, row in df.iterrows():
                    o, h, l, c = row['Open'], row['High'], row['Low'], row['Close']
                    if pd.isna(o) or pd.isna(h) or pd.isna(l) or pd.isna(c):
                        continue
                    change_pct = (c - prev_close) / prev_close * 100
                    bars.append({
                        "time": idx.strftime("%Y-%m-%d %H:%M:%S"),
                        "open": round(float(o), 2),
                        "high": round(float(h), 2),
                        "low": round(float(l), 2),
                        "close": round(float(c), 2),
                        "volume": int(row['Volume']) if not pd.isna(row['Volume']) else 0,
                        "change_pct_from_prev_close": round(float(change_pct), 2)
                    })

                result["indices"][key] = {
                    "symbol": symbol,
                    "prev_close": round(float(prev_close), 2),
                    "5min_bars": bars,
                    "bar_count": len(bars)
                }
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