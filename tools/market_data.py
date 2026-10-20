"""
实时行情获取模块
数据源：新浪财经免费实时接口（反爬极低，毫秒级刷新）
- 上证指数: sh000001
- 中证1000: sh000852
"""

import re
import time
import requests

SINA_URL = "http://hq.sinajs.cn/list={codes}"

# 指数代码映射
INDEX_CODES = {
    "上证指数": "sh000001",
    "中证1000": "sh000852",
}

HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}


def fetch_quotes(codes: list[str]) -> dict[str, dict]:
    """
    批量获取实时行情
    codes: ["sh000001", "sh000852"] 新浪代码格式
    返回: {"上证指数": {"price": xxx, "high": xxx, "low": xxx, ...}, ...}
    """
    url = SINA_URL.format(codes=",".join(codes))
    resp = requests.get(url, headers=HEADERS, timeout=5)
    resp.encoding = "gbk"  # 新浪返回 GBK 编码
    text = resp.text

    result = {}
    # 解析每行: var hq_str_sh000001="...";
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        # 提取引号内的数据
        m = re.search(r'"(.+)"', line)
        if not m:
            continue
        fields = m.group(1).split(",")
        if len(fields) < 30:
            continue
        name = fields[0]
        current_price = float(fields[3]) if fields[3] else 0.0
        if current_price <= 0:
            continue
        high = float(fields[4]) if fields[4] else 0.0
        low = float(fields[5]) if fields[5] else 0.0
        open_price = float(fields[1]) if fields[1] else 0.0
        prev_close = float(fields[2]) if fields[2] else 0.0
        date = fields[30] if len(fields) > 30 else ""
        time_str = fields[31] if len(fields) > 31 else ""

        result[name] = {
            "price": current_price,
            "high": high,
            "low": low,
            "open": open_price,
            "prev_close": prev_close,
            "date": date,
            "time": time_str,
            "change_pct": round((current_price - prev_close) / prev_close * 100, 2) if prev_close else 0.0,
        }
    return result


def get_index_price(index_name: str) -> float | None:
    """获取单个指数的当前价格，返回 None 表示失败"""
    code = INDEX_CODES.get(index_name)
    if not code:
        return None
    try:
        data = fetch_quotes([code])
        return data.get(index_name, {}).get("price")
    except Exception:
        return None


def get_all_prices() -> dict:
    """获取所有关注指数的实时行情"""
    codes = list(INDEX_CODES.values())
    return fetch_quotes(codes)


# ============ 外围市场（美股夜盘 / 亚太 / 欧洲）============
# 新浪国际指数接口，免费不限流、毫秒级。
#   gb_*  : 美股，字段 = 名称, 价格, 涨跌幅%, 时间, ...
#   int_* : 国际指数，字段 = 名称, 价格, 涨跌点数, 涨跌幅%
# 注意：新浪没有 KOSPI，韩股需另找数据源（yfinance 当前被限流）。
OVERSEAS_CODES = {
    "道琼斯": "gb_dji",
    "纳斯达克": "gb_ixic",
    "标普500": "gb_inx",
    "纳斯达克100": "gb_ndx",
    "日经225": "int_nikkei",
    "恒生指数": "int_hangseng",
    "英国富时100": "int_ftse",
}

# 分组，便于"美股夜盘"与"亚太欧洲"分开呈现
US_KEYS = ("道琼斯", "纳斯达克", "标普500", "纳斯达克100")
ASIA_EU_KEYS = ("日经225", "恒生指数", "英国富时100")


def get_overseas_prices() -> dict[str, dict]:
    """获取外围市场行情。

    返回: {"道琼斯": {"name","price","change_pct","time","group"}, ...}
    解析失败或接口异常时返回已成功解析的部分（可能为空 dict）。
    """
    codes = list(OVERSEAS_CODES.values())
    try:
        resp = requests.get(SINA_URL.format(codes=",".join(codes)),
                            headers=HEADERS, timeout=8)
        resp.encoding = "gbk"
        text = resp.text
    except Exception:
        return {}

    result = {}
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        m = re.search(r'"([^"]*)"', line)
        if not m:
            continue
        fields = m.group(1).split(",")
        code = line.split("=")[0].replace("var hq_str_", "").strip()
        name = next((cn for cn, c in OVERSEAS_CODES.items() if c == code), None)
        if not name or not fields or not fields[0]:
            continue
        try:
            if code.startswith("gb_") and len(fields) >= 4:
                result[name] = {
                    "name": fields[0],
                    "price": float(fields[1]),
                    "change_pct": float(fields[2]),
                    "time": fields[3],
                    "group": "us",
                }
            elif code.startswith("int_") and len(fields) >= 4:
                result[name] = {
                    "name": fields[0],
                    "price": float(fields[1]),
                    "change": float(fields[2]),
                    "change_pct": float(fields[3]),
                    "time": "",
                    "group": "asia_eu",
                }
        except (ValueError, IndexError):
            continue
    return result


if __name__ == "__main__":
    # 简单测试
    data = get_all_prices()
    for name, info in data.items():
        print(f"{name}: {info['price']} 涨跌幅:{info['change_pct']}% "
              f"高:{info['high']} 低:{info['low']} 时间:{info['date']} {info['time']}")
