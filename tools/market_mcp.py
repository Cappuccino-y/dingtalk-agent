"""中证1000 行情数据 MCP server。

设计原则：**分析判断交给 OpenCode agent 自己完成**，这里只负责把原料取回来。
唯一的例外是 market_wave —— 它用确定性代码算出道氏/波浪结构（不是黑盒 LLM 分析），
因为波浪理论主观性太强，放给模型自由发挥就会变成"乱数浪"。
  - market_quote    实时行情（新浪，毫秒级）
  - market_overseas 外围市场（美股隔夜 + 日经/恒生/富时，新浪国际接口）
  - market_wave     道氏理论（周线/日线/60分钟三级别 + 指数相互确认）+ 艾略特波浪
  - market_tech     多周期技术面数据（5/15/30/60分钟 + 日线 + 周线）★点位分析原料
  - market_news     近期金十重要快讯

原先的 market_report / market_consult 是"内置 prompt + 再调一次 LLM"的黑盒分析器，
已移除——那套框架改由会话指令交给 agent。

注意：
- 本 server 注册在机器人专属 Location 的 opencode.json 里（见 .opencode-bot/），
  所以只有机器人的会话能看到这些工具，用户平时的 TUI 看不到。
- stdio 传输下 stdout 是协议通道，绝不能被 print 污染：
  所有工具调用都包在 redirect_stdout(sys.stderr) 里。
- 行情/新闻类调用几秒到十几秒，配置里 execution timeout 已放大到 300s。
"""
import contextlib
import logging
import os
import socket
import sys
from datetime import datetime, timedelta
from typing import Annotated, Optional
from urllib.parse import urlparse

# ---- 路径与工作目录：report_news_data 用相对路径写 conversation_history ----
HERE = os.path.dirname(os.path.abspath(__file__))
MAINTOY = os.path.dirname(HERE)
DATALAYER = os.path.join(MAINTOY, "datalayer")
for _p in (MAINTOY, DATALAYER):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.chdir(MAINTOY)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(MAINTOY, ".env"))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s",
                    stream=sys.stderr)
log = logging.getLogger("market_mcp")


def _disable_dead_proxy():
    """系统里的 HTTP_PROXY 常指向没启动的代理（如 Clash），会让所有请求失败。
    启动时探测一次：连不上就清掉，避免行情/新闻全部超时。"""
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        val = os.environ.get(var)
        if not val:
            continue
        try:
            u = urlparse(val)
            with socket.create_connection((u.hostname, u.port or 80), timeout=0.5):
                pass  # 代理活着，保留
        except OSError:
            os.environ.pop(var, None)
            log.warning("代理 %s=%s 连不上 -> 本进程已清空", var, val)


_disable_dead_proxy()

from mcp.server.mcpserver import MCPServer  # noqa: E402
from pydantic import Field  # noqa: E402

mcp = MCPServer(
    name="market_mcp",
    title="中证1000 行情数据",
    description="中证1000 指数（与上证指数）的实时行情、外围市场、道氏+波浪结构、多周期技术面数据、近期重要财经快讯。",
    version="2.0.0",
)

RO = {"read_only_hint": True, "destructive_hint": False,
      "idempotent_hint": False, "open_world_hint": True}


@contextlib.contextmanager
def _quiet_stdout():
    """把工具执行期间的 stdout 重定向到 stderr，保护 stdio 协议通道。"""
    with contextlib.redirect_stdout(sys.stderr):
        yield


# ---------------------------------------------------------------- 1) 实时行情
@mcp.tool(
    name="market_quote",
    title="实时行情",
    description=(
        "毫秒级获取中证1000指数与上证指数的实时行情（现价、涨跌幅、开高低、前收、时间）。"
        "**只要数字（『现在多少点』『今天涨跌多少』）就用这个，秒回。**"
        "需要做点位/策略判断时，配合 market_tech（技术面）和 market_news（消息面）使用。"
    ),
    annotations={**RO, "idempotent_hint": True},
)
def market_quote() -> str:
    """返回两个指数的实时行情（纯文本，一行一个指数）。"""
    with _quiet_stdout():
        from tools.market_data import get_all_prices
        data = get_all_prices()
    if not data:
        return "获取行情失败（休市或接口异常）。"
    lines = []
    for name, d in data.items():
        lines.append(
            f"{name}：{d['price']}（{d['change_pct']:+.2f}%）"
            f"　开 {d['open']} / 高 {d['high']} / 低 {d['low']} / 前收 {d['prev_close']}"
            f"　{d['date']} {d['time']}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------- 2) 外围市场
@mcp.tool(
    name="market_overseas",
    title="外围市场行情",
    description=(
        "获取外围市场最新行情：**美股隔夜收盘**（道琼斯/纳斯达克/标普500/纳斯达克100，含收盘时间），"
        "以及日经225、恒生指数、英国富时100。"
        "**做市场走势/点位/策略分析时必须先看这个** —— 隔夜美股的方向和幅度会直接决定 A 股开盘情绪，"
        "这是判断『今天该做多还是做空』的第一手依据。毫秒级返回。"
        "（新浪无 KOSPI 数据，韩股暂缺。）"
    ),
    annotations={**RO, "idempotent_hint": True},
)
def market_overseas() -> str:
    """返回外围市场行情，分「美股隔夜」和「亚太/欧洲」两组。"""
    with _quiet_stdout():
        from tools.market_data import get_overseas_prices
        data = get_overseas_prices()
    if not data:
        return "获取外围行情失败。"
    us, ae = [], []
    for k, v in data.items():
        if v.get("group") == "us":
            us.append(f"- {k}：{v['price']:.2f}（{v['change_pct']:+.2f}%）{v.get('time', '')}".rstrip())
        else:
            ae.append(f"- {k}：{v['price']:.2f}（{v['change_pct']:+.2f}%）")
    parts = []
    if us:
        parts.append("**美股隔夜**\n" + "\n".join(us))
    if ae:
        parts.append("**亚太 / 欧洲**\n" + "\n".join(ae))
    return "\n\n".join(parts)


# ---------------------------------------------------------------- 3) 多周期技术面
def _strip_dead_asian_section(text: str) -> str:
    """baostock_daily 会在开头插一段「日韩盘面」（走 yfinance，当前被 Yahoo 限流恒失败）。
    外围数据已由 market_overseas 工具提供，这里把这段噪声去掉，避免误导 agent。"""
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        if ln.startswith("# ") and "多周期技术面分析数据" in ln:
            return "\n".join(lines[i:]).strip() if i else text
    return text


@mcp.tool(
    name="market_tech",
    title="多周期技术面数据",
    description=(
        "获取指数的多周期技术面数据：5分钟 / 15分钟 / 30分钟 / 60分钟 / 日线 / 周线，"
        "包含各周期均线、K线、量价、MACD、布林带等关键数据。"
        "**这是做点位分析（支撑位/压力位/进场区/止损止盈）以及道氏理论/波浪理论分析的必需原料。**"
        "道氏理论的『指数相互确认』要求对比中证1000与上证指数，"
        "所以判断趋势时**两个指数都要取**（分别调用一次，指定 index_name）。"
        "返回原始数据，**分析判断由你自己完成**。耗时约 5~20 秒。"
    ),
    annotations=RO,
)
def market_tech(
    index_name: Annotated[str, Field(
        description='指数名称，只支持 "中证1000" 或 "上证指数"。默认 中证1000。',
        default="中证1000",
    )] = "中证1000",
) -> str:
    """返回指定指数的多周期技术面数据文本（原始数据，不含结论）。"""
    if index_name not in ("中证1000", "上证指数"):
        return f'不支持的指数：{index_name}（只支持 "中证1000" 或 "上证指数"）'
    with _quiet_stdout():
        from baostock_daily import IndexDataFetcher
        with IndexDataFetcher() as fetcher:
            text = fetcher.generate_ai_prompt(
                index_name=index_name,
                periods=["5min", "15min", "30min", "60min", "日线", "周线"],
            )
    text = (text or "").strip()
    return _strip_dead_asian_section(text) or f"{index_name} 技术面数据获取失败。"


# ---------------------------------------------------------------- 3) 道氏 + 波浪结构
@mcp.tool(
    name="market_wave",
    title="道氏理论 + 艾略特波浪结构",
    description=(
        "**确定性计算**的道氏理论 + 艾略特波浪结构分析，做走势/点位判断时优先用它。"
        "道氏按『三个级别』给出：周线（主要趋势）、日线（次级趋势）、60分钟（短期结构），"
        "每级含主/次级趋势、相位、量能确认、趋势延续或破坏信号，"
        "并做『指数相互确认』（中证1000 vs 上证指数）。"
        "波浪用百分比 ZigZag 找摆动点，按 5 浪推动模板匹配并逐条校验三大铁律"
        "（浪2不破起点／浪3不是最短／浪4不进浪1区域）与斐波那契浪间关系，"
        "输出当前浪位、目标位、失效位、置信度以及备选数浪。"
        "**关键：这些点位已经算好了，你只负责解读和叙述——绝对不要自己重新数浪或另算点位。**"
        "若返回『不构成有效推动浪结构』，请如实这么说，不要硬凑浪型。"
        "耗时约 20~60 秒。"
    ),
    annotations={**RO, "idempotent_hint": True},
)
def market_wave(
    index_name: Annotated[str, Field(
        description='指数名称，只支持 "中证1000" 或 "上证指数"。默认 中证1000。',
        default="中证1000",
    )] = "中证1000",
) -> str:
    """返回道氏（三级别）+ 艾略特波浪的确定性计算结果。"""
    if index_name not in ("中证1000", "上证指数"):
        return f'不支持的指数：{index_name}（只支持 "中证1000" 或 "上证指数"）'
    with _quiet_stdout():
        from tools.wave_engine import analyze
        return analyze(index_name)


# ---------------------------------------------------------------- 4) 近期快讯
@mcp.tool(
    name="market_news",
    title="近期重要财经快讯",
    description=(
        "获取最近若干小时的「金十」重要财经快讯（消息面）。"
        "做市场/点位分析时用它补充消息面依据；纯查行情数字不需要。"
        "返回原始快讯列表，**影响判断由你自己完成**。耗时约 3~10 秒。"
    ),
    annotations=RO,
)
def market_news(
    hours_back: Annotated[int, Field(
        description="回溯小时数，默认 12（通常覆盖隔夜到当前）。范围 1~72。",
        ge=1, le=72, default=12,
    )] = 12,
) -> str:
    """返回近 N 小时的重要快讯文本。"""
    with _quiet_stdout():
        from report_news_data import fetch_jin10_important_news
        end = datetime.now()
        start = end - timedelta(hours=hours_back)
        items = fetch_jin10_important_news(
            start.strftime("%Y-%m-%d %H:%M:%S"),
            end.strftime("%Y-%m-%d %H:%M:%S"),
        ) or []
    if not items:
        return f"（近 {hours_back} 小时无重要快讯）"
    lines = []
    for it in items[:30]:
        t = it.get("time", "")
        content = (it.get("data") or {}).get("content", "")
        lines.append(f"- {t} {content}".rstrip())
    return "\n".join(lines)


if __name__ == "__main__":
    log.info("market_mcp starting (cwd=%s)", os.getcwd())
    mcp.run(transport="stdio")
