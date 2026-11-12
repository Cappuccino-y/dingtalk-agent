"""
IC 期货多信号监控（抄底 + 顶部预警/确认）—— **由本 bot 自主管理（2026-10-23 收回）**

IC 监控整条链归本机器人（用户 2026-10-23 明确：「这个 icmonitor 完全由你控制」）：
- 定时：本仓库调度器作业 `ic_monitor`（08:50，**每天都跑**）/ `ic_recheck`（09:20，每天）/ `ic_weekly`（周一 09:05）
- 周末行为（2026-10-24 用户要求）：周末照跑，判的是**最新交易日**的数据
  （周六早上 akshare 最新数据 = 周五收盘），推一份「非交易日回顾」。
- 推送节奏（2026-10-25 用户反馈「今天早上又没有预报」后改）：**每天早上必有一条**。
  心跳按**运行日**去重（不再按数据日），信号按**数据日**去重（同一条信号不重推）；
  行情源比交易日历慢时，消息里明说「应到 X / 实际到 Y」。
- 推送：**本机器人 1:1 通道**（tools.dingtalk_push）→ 署名是本机器人，不是「py 小助手」群机器人
- 状态/历史：本仓库 `var/ic/`（已并入 MainToy 那条链的历史，10-22 那条抄底不会重推）
- 手动入口：`/ic`（聊天）或 `python -m scheduler run ic_monitor`（命令行，作业 id 只能用 id）
- 另：`MainToy\autorun\autoworkflow.py` 已不再拉起 IC 脚本，不会双推

想**额外**推「交易讯息更新」群（py 小助手 webhook）：设环境变量 `IC_PUSH_GROUP=1`（默认只推 1:1）。

信号体系（2015-2026 回测验证，2026-10-23 加了三条防误判规则）:

【抄底信号】RSI<44 + QVIX 5d<-3% + vol>20% + 融资20日降
  → 恐慌释放 + 杠杆出清 → 做多
  **强度分级**：融资20日 ≤-3% = 强（真实杠杆出清）；>-3% = 弱（趋势性降杠杆，只作观察）

【顶部预警】60日新高 + 3日跌>3% + 融资>90%
  历史: 12次, 83.3% 后续 5-20 日跌>5%
  → 高位确认 → 做空预警

【顶部确认】顶部预警 + QVIX 3日涨>0%
  历史: 5次, 100% 后续 5-20 日跌>5%
  → 恐慌加剧 → 做空确认

三条防误判规则（2026-10-23 加，起因见下面「已知误判案例」）：

1. **QVIX 窗口缺日 → 不判定**：`pct_change(5)` 是位置位移，QVIX 序列缺行时
   "5 天前"会落到不相干的一天（2026-09-10/11 两天 QVIX 缺失 → 09-11、10-12
   那两次触发的"5日动量"今天都算不出来）。缺数据 = 不判定，不是 = 触发。
2. **长假前夜 → 顺延到节后重判**：最新数据日若紧跟 ≥3 天连续休市（长假前夜），
   本轮不判定，等节后第一个交易日重判。2026-09-24 那次就是这么被拦掉的。
3. **融资强度分级**：不设 -3% 硬门槛（实测会剔掉 9 簇里的 6 簇，其中 5 簇是
   20/60 日都涨的有效样本），只分级标注。

**已知误判案例**：2026-09-24（中秋前最后一个交易日）那次抄底 —— 指数 -2.15%
是节前流动性枯竭的抛售、QVIX 几乎没反映（23.30→23.98），而"5日动量 -7.38%"
测的是 6 天前一轮**反弹期**的恐慌溢价消退（同期指数还涨到 7850）。节后重判
（09-28~10-09 连续 5 个交易日）均不成立 → 规则 2 生效。

时间说明：akshare 当天数据在收盘后 3-5 小时（约 18:00 后）才更新，
次日 8:50 跑相当于"开盘前预警"，拿到的是前一天的完整收盘数据。
"""
import sys
import os
import time
import json
import argparse
import logging
import akshare as ak
from datetime import date, datetime, timedelta
from pathlib import Path
import pandas as pd
import numpy as np

# 本仓库根目录（dingtalk-agent）。作业脚本放在 jobs/ 下，所以上两级就是仓库根。
HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for _p in (REPO, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# ---------------------------------------------------------------- 推送出口
# 2026-10-23：IC 信号改由**本机器人**发出（1:1 通道，署名本机器人）。
# 之前走「交易讯息更新」群的 py 小助手 webhook（tools.notifycation）——那是 MainToy 那条链的出口；
# 现在整条 IC 链归本 bot，启动、状态、去重、推送都是自己说了算。
PUSH_GROUP = os.environ.get("IC_PUSH_GROUP", "").strip().lower() in ("1", "true", "yes", "on")

# ---------------------------------------------------------------- 编码兜底
# Windows 上 stdout/stderr 默认是 GBK(cp936)：emoji（🚨）**根本编不出去**，
# print 一旦带上 emoji 就会抛 UnicodeEncodeError → 整轮退出码 1
# （推送其实早成功了，却白被判成 failed，2026-10-22 08:50 就是这么炸的）。
# 这里统一切到 UTF-8 且 errors="replace"，保证再怪异的字符也不会抛异常。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def emit_signal(payload: dict) -> None:
    """把信号打到 stdout，供 autoworkflow 捕获。

    **必须 ensure_ascii=True**：转义成 \\uXXXX 后 stdout 全是 ASCII 字节，
    下游无论按 GBK 还是 UTF-8 解码都不会乱，json.loads 拿到的对象完全一致；
    直接输出中文/emoji 则会被编码问题打断整轮流程。
    """
    try:
        print(f"SIGNAL_TRIGGERED:{json.dumps(payload, ensure_ascii=True)}")
        sys.stdout.flush()
    except Exception as e:  # noqa: BLE001
        logging.error("输出 SIGNAL_TRIGGERED 失败：%s", e)


def push_bot_message(text: str, title: str = "IC 信号") -> bool:
    """把信号推到用户钉钉 1:1 会话（**本机器人**署名）。

    - 主通道：tools.dingtalk_push.push_markdown（机器人 1:1，长期有效）
    - 可选：IC_PUSH_GROUP=1 时**额外**推一次「交易讯息更新」群（py 小助手 webhook）
    """
    ok = False
    try:
        from tools.dingtalk_push import push_markdown
        ok = bool(push_markdown(text, title=title))
    except Exception as e:  # noqa: BLE001
        logging.error(f"1:1 推送异常（{title}）：{e}")
    if not ok:
        logging.error(f"1:1 推送失败（{title}）")

    if PUSH_GROUP:
        try:
            from tools.notifycation import send_dingtalk_message
            send_dingtalk_message(text)
        except Exception as e:  # noqa: BLE001
            logging.error(f"群 webhook 推送异常（{title}）：{e}")
    return ok


# 运行期产物放 var/ic/（.gitignore 已忽略 var/），不污染源码目录
VAR_DIR = REPO / "var" / "ic"
VAR_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = VAR_DIR / "ic_signal_monitor.log"
LATEST_SIGNAL = VAR_DIR / "ic_latest_signal.json"
SIGNAL_HISTORY = VAR_DIR / "ic_signal_history.json"
# 每日去重状态：--once 模式（机器人 08:50 自动调度）靠它避免重启后重复推送
STATE_PATH = VAR_DIR / "ic_monitor_state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)


def _current_ic_contract() -> str:
    """返回当前 IC 主力合约代码（最近季月）"""
    now = datetime.now()
    year = now.year
    month = now.month
    # 季月: 03, 06, 09, 12
    for m in [3, 6, 9, 12]:
        if month <= m:
            return f"IC{year % 100:02d}{m:02d}"
    return f"IC{(year + 1) % 100:02d}03"


def load_latest_data():
    """实时加载中证500指数(指标) + IC期货(价格参考) + 500ETF QVIX + 融资余额"""
    from datalayer.akshare_helper import fetch_qvix

    # 中证500指数日线（用于 RSI / 波动率 — 无换月问题）
    logging.info("拉取中证500指数(000905)日线...")
    spot = ak.stock_zh_index_daily(symbol="sh000905")
    if spot is None or spot.empty:
        raise RuntimeError("akshare 返回空中证500指数数据")
    spot["date"] = pd.to_datetime(spot["date"])
    spot = spot.rename(columns={"close": "spot"}).sort_values("date").reset_index(drop=True)

    # IC 期货（仅做价格参考）
    code = _current_ic_contract()
    logging.info(f"拉取 {code} 日线（价格参考）...")
    fut = ak.futures_zh_daily_sina(symbol=code)
    if fut is None or fut.empty:
        raise RuntimeError(f"akshare 返回空数据: {code}")
    fut["date"] = pd.to_datetime(fut["date"])
    fut = fut.rename(columns={"close": "fut"}).sort_values("date").reset_index(drop=True)

    # QVIX
    logging.info("拉取 500ETF QVIX...")
    # 开盘前先补采上一交易日：15:05 那轮被源站「没更新」挡掉时，这里是唯一的
    # 补救窗口（源站当晚/次日凌晨才更新分钟文件的情况常有）。见 qvix_store。
    if os.environ.get("IC_QVIX_CATCHUP", "1").strip() != "0":
        try:
            from datalayer.qvix_store import catchup_prev_session
            _cu = catchup_prev_session("500etf")
            if _cu.get("ok"):
                logging.warning(f"QVIX 开盘前补采成功：{_cu['date']} close={_cu['close']}")
            elif _cu.get("date"):
                logging.info(f"QVIX 开盘前补采：{_cu['date']} 未补（{_cu.get('reason')}）")
            else:
                logging.info(f"QVIX 开盘前补采跳过：{_cu.get('reason')}")
        except Exception as e:  # noqa: BLE001
            logging.warning(f"QVIX 开盘前补采异常（忽略）: {e}")
    # 缺口代理补齐（2026-11-09）：源站会整天不更新（11-09 分钟文件还是 11-06 那份），
    # 这时用上交所官方 IV 自算 + 锚定链式把最近的缺口补上，否则判据因为缺一天整轮拒判。
    # 幂等：没缺口时直接返回，不打官方接口。见 datalayer/qvix_proxy.py。
    if os.environ.get("IC_QVIX_PROXY_FILL", "1").strip() != "0":
        try:
            from datalayer.qvix_proxy import SELF_ROOTS, fill_gaps
            for _qt in SELF_ROOTS:
                _pr = fill_gaps(_qt)
                if _pr.get("ok"):
                    logging.warning(f"QVIX {_qt} 代理补缺口："
                                    f"{', '.join(_pr['written'])}")
        except Exception as e:  # noqa: BLE001
            logging.warning(f"QVIX 代理补缺口异常（忽略）: {e}")
    try:
        qvix = fetch_qvix("500etf")
    except Exception as e:  # noqa: BLE001
        # 源站挂 + 本地无快照（首次部署才会这样）：不整体崩，按「QVIX 缺」处理，
        # 让数据守门判拒判 —— 宁可明确说缺数据，也不拿旧值冒充新值。
        logging.error(f"QVIX 拉取失败，本日按缺失处理：{e}")
        qvix = None
    if qvix is None or qvix.empty or "close" not in qvix.columns:
        logging.warning("QVIX 无可用数据 → qvix 列全空，判定会走「拒判」")
        qvix = pd.DataFrame({
            "date": pd.to_datetime(pd.Series([], dtype="object")),
            "close": pd.Series([], dtype="float64"),
        })
    qvix["date"] = pd.to_datetime(qvix["date"])

    # 融资余额（沪市，2015 起）
    logging.info("拉取沪市融资余额...")
    margin = ak.stock_margin_sse(start_date="20150101", end_date="20270101")
    margin = margin.rename(columns={"信用交易日期": "date", "融资余额": "margin"})
    margin["date"] = pd.to_datetime(margin["date"])
    margin = margin[["date", "margin"]].sort_values("date")

    # 合并
    df = spot[["date", "spot"]].merge(fut[["date", "fut"]], on="date", how="left")
    df = df.merge(qvix[["date", "close"]], on="date", how="left")
    df = df.rename(columns={"close": "qvix"})
    df = df.merge(margin, on="date", how="left")
    df = df.sort_values("date").reset_index(drop=True)
    # 官方 QVIX 缺最新日时的自算兜底（只进 qvix_self 列，仅参考）
    df = _attach_self_qvix(df)
    return df


def compute_rsi14(close: pd.Series, period: int = 14) -> pd.Series:
    """标准 RSI(14)"""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - 100 / (1 + rs)


def compute_vol_60d(fut: pd.Series, window: int = 60) -> pd.Series:
    """60 日年化波动率（%）"""
    log_ret = np.log(fut / fut.shift(1))
    return log_ret.rolling(window).std() * np.sqrt(252) * 100


def _attach_self_qvix(df: pd.DataFrame, lookback: int = 6) -> pd.DataFrame:
    """官方 QVIX 缺最新日时，用上交所官方 IV 自算一份**仅供参考**的尾部。

    2026-10-26：optbbs 源站 10-23 起不回填，卡片只能显示「缺」。自算精度实测
    （72 个交易日）：水位 +4.2%±5.8%、5 日动量相关 0.83、<-3% 阈值一致 81%
    —— **够当参考，不够当判据**。所以这里只写进独立列 `qvix_self`，
    绝不碰 `qvix` 列（碰了就等于偷偷改了判定依据和历史回测口径）。
    """
    df = df.copy()
    df["qvix_self"] = np.nan
    if os.environ.get("IC_QVIX_SELF_REF", "1").strip() == "0":
        return df
    if df.empty or pd.notna(df.iloc[-1]["qvix"]):
        return df
    try:
        from datalayer.qvix_selfcalc import compute_cached
    except Exception as e:  # noqa: BLE001
        logging.warning(f"自算 QVIX 模块不可用: {e}")
        return df
    filled = 0
    for idx, row in df.tail(lookback).iterrows():
        try:
            v = compute_cached(row["date"].date())
        except Exception as e:  # noqa: BLE001
            logging.warning(f"自算 QVIX {row['date'].date()} 失败: {e}")
            v = None
        if v is not None:
            df.loc[idx, "qvix_self"] = float(v)
            filled += 1
    if filled:
        logging.info(f"自算 QVIX 兜底：填了 {filled} 天（仅参考，不参与判定）")
    return df


def load_history() -> list:
    """读取历史触发记录"""
    if SIGNAL_HISTORY.exists():
        try:
            return json.loads(SIGNAL_HISTORY.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_history(history: list):
    """保存历史触发记录"""
    SIGNAL_HISTORY.write_text(
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def get_last_trigger_date(current_date_str: str, sig_type: str = "bottom") -> str:
    """获取上次触发日期（不包含今日），按类型过滤"""
    history = load_history()
    if not history:
        return "（首次触发，无历史）"
    for entry in reversed(history):
        if entry.get("type", "bottom") != sig_type:
            continue
        if entry.get("date") != current_date_str:
            return entry.get("date", "未知")
    return "（首次触发，无历史）"


def already_alerted(current_date_str: str, sig_type: str) -> bool:
    """今日该类型信号是否已写入历史（10分钟循环内防重复通知）"""
    history = load_history()
    return any(
        e.get("date") == current_date_str and e.get("type", "bottom") == sig_type
        for e in history
    )


def _mom_with_gap_guard(s: pd.Series, n: int) -> pd.Series:
    """n 日动量，但**窗口内任一天缺数据 → 该日动量无效（NaN）**。

    为什么不能直接用 `s.pct_change(n)`：那是**位置位移**。QVIX 序列缺行时
    （2026-09-10、09-11 两天就缺），"n 天前"会落到不相干的那一天 ——
    09-11、10-12 那两次触发就是这么来的，今天按同样的数据算根本复现不出来。
    缺数据必须 = 不判定，不能 = 触发。
    """
    out = np.full(len(s), np.nan, dtype="float64")
    vals = s.to_numpy(dtype="float64")
    for i in range(n, len(vals)):
        win = vals[i - n: i + 1]
        if np.isnan(win).any():
            continue
        base = win[0]
        if base <= 0:
            continue
        out[i] = win[-1] / base - 1.0
    return pd.Series(out, index=s.index)


def prepare_signals(df: pd.DataFrame) -> pd.DataFrame:
    """计算所有指标列"""
    df = df.copy()
    df["rsi14"] = compute_rsi14(df["spot"], 14)
    # fill_method=None: 数据源滞后产生NaN时动量=NaN→条件不满足
    # (默认pad会用旧值填充NaN, 用陈旧数据算出假动量, 2026-10-12假触发根因)
    # + 缺口守门: 窗口内缺日 → 动量无效（规则 1）
    df["qvix_mom_3d"] = _mom_with_gap_guard(df["qvix"], 3)
    df["qvix_mom_5d"] = _mom_with_gap_guard(df["qvix"], 5)
    # 自算参考列（存在才算；口径同上，窗口缺一天就算不出来）
    if "qvix_self" in df.columns:
        df["qvix_self_mom_5d"] = _mom_with_gap_guard(df["qvix_self"], 5)
    else:
        df["qvix_self_mom_5d"] = np.nan
    df["vol_60d"] = compute_vol_60d(df["spot"], 60)
    df["margin_mom20"] = df["margin"].pct_change(20, fill_method=None)
    df["margin_pct"] = df["margin"].rolling(250).apply(
        lambda x: (x.iloc[-1] >= x).mean(), raw=False)
    # 60 日新高（前一日为参考，今日创新高）
    df["high60_prev"] = df["spot"].rolling(60, min_periods=60).max().shift(1)
    df["is_high"] = df["spot"] > df["high60_prev"]
    return df


# ---------------------------------------------------------------- 交易日历
# 行情日线里**没有假日的行**，光看指数数据看不出"节前"。
# 判断长假前夜必须查交易日历（akshare 一次拉全量，缓存 6 小时）。
_TRADE_CAL: set | None = None
_TRADE_CAL_TS: float = 0.0
_PRE_HOLIDAY: set | None = None
_PRE_HOLIDAY_TS: float = 0.0

LONG_HOLIDAY_GAP_DAYS = 4   # 数据日 -> 下一个交易日 的自然日间隔 >= 4 → 后面紧跟长假


def _trade_calendar() -> set:
    """A 股交易日历（date 集合）。拉不到就返回空集 → 节前判断降级。"""
    global _TRADE_CAL, _TRADE_CAL_TS
    now = time.time()
    if _TRADE_CAL is not None and now - _TRADE_CAL_TS < 6 * 3600:
        return _TRADE_CAL
    try:
        cal_df = ak.tool_trade_date_hist_sina()
        cal = {pd.Timestamp(d).date() for d in cal_df["trade_date"]}
        _TRADE_CAL, _TRADE_CAL_TS = cal, now
        logging.info(f"交易日历已加载（{len(cal)} 天）")
        return cal
    except Exception as e:  # noqa: BLE001
        logging.warning(f"拉交易日历失败（{e}），节前顺延判断降级为不启用")
        return set()


def pre_long_holiday_dates() -> set:
    """所有"长假前夜"的交易日（集合，缓存 6 小时）。

    判据：下一个交易日 - 该交易日 >= 4 个自然日
    （普通周末 = 3 天：周五→周一；长假前夜 >= 4 天，如中秋前的周四→下周一）。
    批量场景（回测/周报）用这个集合，别逐行调 is_pre_long_holiday。
    """
    global _PRE_HOLIDAY, _PRE_HOLIDAY_TS
    now = time.time()
    if _PRE_HOLIDAY is not None and now - _PRE_HOLIDAY_TS < 6 * 3600:
        return _PRE_HOLIDAY
    cal = sorted(_trade_calendar())
    out = {d for i, d in enumerate(cal[:-1])
           if (cal[i + 1] - d).days >= LONG_HOLIDAY_GAP_DAYS}
    _PRE_HOLIDAY, _PRE_HOLIDAY_TS = out, now
    return out


def is_pre_long_holiday(d) -> bool:
    """这个交易日是不是"长假前夜"（后面紧跟 >=3 天连续休市）。

    为什么要有这条：2026-09-24（中秋前最后一个交易日）那次抄底是假信号 ——
    当天指数 -2.15% 是节前流动性枯竭的抛售、QVIX 几乎没定价（23.30→23.98），
    而"5日动量 -7.38%"测的是 6 天前一轮**反弹期**的恐慌溢价消退。
    节后第一个交易日重判时（09-28~10-09 连续 5 日）条件都不成立 → 这次信号就不该发。
    """
    d0 = d.date() if hasattr(d, "date") else d
    return d0 in pre_long_holiday_dates()


# ------------------------------------------------------------ 数据新鲜度
def latest_closed_trading_day(now: datetime | None = None) -> date | None:
    """按交易日历推：截至现在，最近一个**已经收盘**的交易日（不含今天）。

    行情源比日历慢是常事（akshare 当天数据收盘后 3-5 小时才更新，QVIX 更慢，
    实测能差 1 个交易日）。消息里必须明说结论基于哪一天，别让用户以为是实时数据。
    """
    cal = _trade_calendar()
    if not cal:
        return None
    now = now or datetime.now()
    past = [d for d in cal if d < now.date()]
    return max(past) if past else None


def data_lag_note(data_date_str: str, now: datetime | None = None) -> str:
    """行情源滞后提示：应到 X，实际只取到 Y。"""
    try:
        data_date = datetime.strptime(str(data_date_str), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return ""
    latest = latest_closed_trading_day(now)
    if latest is None or data_date >= latest:
        return ""
    return (f"\n> ℹ️ **行情源滞后**：按交易日历最新应到 **{latest}**，"
            f"实际只取到 **{data_date}** —— 本条结论基于 {data_date} 收盘数据")


# ------------------------------------------------------------ 信号强度分级
STRONG_MARGIN_MOM = -0.03   # 融资余额 20 日动量 <= -3% → 强信号


def signal_strength(result: dict) -> tuple:
    """给抄底信号分级 → (等级, 说明)。

    强：融资 20 日动量 ≤ -3% → 真实杠杆出清
    弱：融资 20 日动量 在 (-3%, 0) → 趋势性降杠杆（节前/月末常见），证据偏弱

    为什么分级而不是设 -3% 硬门槛：实测硬门槛会剔掉 9 簇里的 6 簇，
    其中 5 簇是 20/60 日都上涨的有效样本（2022-10-31 / 2024-12-31 / 2025-01-09 /
    2025-11-25 / 2026-03-25）—— 那是"删好样本"，不是"去坏样本"，
    而且它挡不住 10-12 那次假触发（那次融资 -3.19% 照样通过）。
    """
    m = result.get("margin_mom20")
    try:
        m = float(m)
    except (TypeError, ValueError):
        return "未知", "融资数据缺失"
    if m != m:  # NaN
        return "未知", "融资数据缺失"
    if m <= STRONG_MARGIN_MOM:
        return "强", f"融资20日 {m:.2%}（≤-3%，真实杠杆出清）"
    return "弱", f"融资20日 {m:.2%}（>-3%，趋势性降杠杆，证据偏弱）"


def _missing_qvix_dates(df: pd.DataFrame, tail_rows: int = 6) -> list[str]:
    """最近几行里 QVIX 为空（= 当天源站没发布官方值）的交易日，给卡片用。

    2026-11-09：卡片原来只写一个笼统的「数据不齐 / QVIX窗口缺日」，用户看不出
    到底缺哪天、也判断不了是源站停更还是本地坏了。这里把具体日期摆出来。
    """
    if df is None or df.empty or "qvix" not in df.columns:
        return []
    tail = df.tail(tail_rows)
    return [d.strftime("%Y-%m-%d") for d in tail.loc[tail["qvix"].isna(), "date"]]


def _qvix_reference(df: pd.DataFrame, col: str) -> tuple[float | None, str | None, int | None]:
    """最新交易日 QVIX 缺失时，给出「最近一个可算的动量 + 它的日期 + 滞后几行」。

    只用于**显示**（卡片上把「缺」换成「截至 10-22 的动量」），
    不参与触发判定 —— 判定口径仍然要求最新交易日的数据齐全。

    返回 (mom, date_str, lag_rows)；整段都算不出动量时返回 (None, None, None)。
    """
    valid = df[df[col].notna()]
    if valid.empty:
        return None, None, None
    last = valid.iloc[-1]
    try:
        pos = int(df.index.get_loc(valid.index[-1]))
    except Exception:  # noqa: BLE001
        pos = len(valid) - 1
    lag = max(len(df) - 1 - pos, 0)
    return float(last[col]), last["date"].strftime("%Y-%m-%d"), lag


def check_bottom_signal(df: pd.DataFrame) -> dict:
    """抄底信号: RSI<44 + QVIX 5d<-3% + vol>20% + 融资20日降"""
    latest = df.iloc[-1]
    # 数据完整性守门: 任一关键数据缺失/滞后 → 本轮**拒判**(2026-10-12假触发教训)。
    # ⚠️ 但守门要**按指标分开算**（2026-10-25 修）：老写法是「只要 stale 非空，
    # qvix_ok 和 margin_ok 一起打成 False」—— QVIX 缺一天，融资那项明明
    # -7.25% < 0 满足条件，也被显示成 ❌，用户看着像"条件不满足"。
    # 现在：每个指标只受**自己那侧**的数据缺失影响；整体是否拒判仍看全部四项
    # （data_ok），判定口径不变（保持严格）。
    qvix_bad: list[str] = []
    margin_bad: list[str] = []
    if pd.isna(latest["qvix"]):
        qvix_bad.append("QVIX源未回填")
    if pd.isna(latest["margin_mom20"]):
        margin_bad.append("融资余额")
    # 规则 1：QVIX 窗口内有缺日 → 5 日动量不可算 → 本轮不判定（并说明原因）
    if pd.notna(latest["qvix"]) and pd.isna(latest["qvix_mom_5d"]):
        qvix_bad.append("QVIX窗口缺日")
    qvix_last_date = df.loc[df["qvix"].notna(), "date"].iloc[-1] if df["qvix"].notna().any() else None
    if qvix_last_date is not None and (latest["date"] - qvix_last_date).days >= 2:
        qvix_bad.append(f"QVIX滞后{(latest['date'] - qvix_last_date).days}天")
    margin_last_date = df.loc[df["margin"].notna(), "date"].iloc[-1] if df["margin"].notna().any() else None
    if margin_last_date is not None and (latest["date"] - margin_last_date).days >= 2:
        margin_bad.append(f"融资滞后{(latest['date'] - margin_last_date).days}天")
    stale = qvix_bad + margin_bad
    if stale:
        logging.warning(f"数据完整性守门触发({', '.join(stale)})，缺的那一项按「无法确认」处理")
    rsi_ok = float(latest["rsi14"]) < 44
    qvix_ok = (not qvix_bad) and pd.notna(latest["qvix_mom_5d"]) and float(latest["qvix_mom_5d"]) < -0.03
    vol_ok = float(latest["vol_60d"]) > 20
    margin_ok = (not margin_bad) and pd.notna(latest["margin_mom20"]) and float(latest["margin_mom20"]) < 0
    stale_reason = "、".join(stale) if stale else ""
    qvix_ref_mom, qvix_ref_date, qvix_ref_lag = _qvix_reference(df, "qvix_mom_5d")

    # 2026-11-09：把「拒判」拆成两种完全不同的情况（用户反馈「数据怎么又不齐了」）。
    #   ① hard_bad：连指数/RSI/波动率都算不出来 → **真的没法判**，保持拒判；
    #   ② 只有 QVIX / 融资缺 → 那一项「无法确认」，但其余几项照常判，
    #      结论写成「未触发（条件X 无法确认）」；其余项全满足时升级成「⚠️待确认」。
    # 为什么要改：缺一天 QVIX，RSI/波动率/融资三项明明算得出来且都**不满足**
    # （离触发很远），整张卡却写「⚠️拒判（数据不齐）」= 一张纯废卡。
    # 注意：改的只是**措辞与是否整轮作废**，判定口径一点没松 ——
    # 缺数据永远不可能变成「触发」，最多变成「待确认」。
    hard_bad = bool(pd.isna(latest["rsi14"]) or pd.isna(latest["vol_60d"]))
    known_ok = [rsi_ok, vol_ok]
    if not qvix_bad:
        known_ok.append(qvix_ok)
    if not margin_bad:
        known_ok.append(margin_ok)
    pending_confirm = bool((qvix_bad or margin_bad) and not hard_bad
                           and all(known_ok))
    return {
        "date": latest["date"].strftime("%Y-%m-%d"),
        "spot_price": float(latest["spot"]),
        "fut_price": float(latest["fut"]) if pd.notna(latest.get("fut")) else float(latest["spot"]),
        "rsi14": float(latest["rsi14"]),
        "qvix": float(latest["qvix"]),
        "qvix_mom_5d": float(latest["qvix_mom_5d"]) if pd.notna(latest["qvix_mom_5d"]) else float("nan"),
        # 参考值：最新日缺 QVIX 时，卡片显示「截至上一个有效日」的动量（仅供参考，不参与判定）
        "qvix_mom_5d_ref": qvix_ref_mom,
        "qvix_ref_date": qvix_ref_date,
        "qvix_ref_lag": qvix_ref_lag,
        # 自算兜底（2026-10-26 起；**仅参考**，不参与判定）
        "qvix_self_val": float(latest["qvix_self"]) if pd.notna(latest.get("qvix_self")) else float("nan"),
        "qvix_self_mom_5d": float(latest["qvix_self_mom_5d"]) if pd.notna(latest.get("qvix_self_mom_5d")) else float("nan"),
        "vol_60d": float(latest["vol_60d"]),
        "margin_mom20": float(latest["margin_mom20"]) if pd.notna(latest["margin_mom20"]) else float("nan"),
        "rsi_ok": bool(rsi_ok),
        "qvix_ok": bool(qvix_ok),
        "vol_ok": bool(vol_ok),
        "margin_ok": bool(margin_ok),
        # 各项数据是否齐全（显示时区分「不满足」和「数据缺失」）
        "qvix_has_data": bool(not qvix_bad),
        "margin_has_data": bool(not margin_bad),
        "missing_qvix_dates": _missing_qvix_dates(df),
        "hard_bad": hard_bad,                 # True 才是真·拒判（连指数/RSI 都算不出）
        "pending_confirm": pending_confirm,   # 其余条件已满足、只差缺数据那一项
        "triggered": bool(rsi_ok and qvix_ok and vol_ok and margin_ok),
        "data_ok": bool(not stale),
        "stale_reason": stale_reason,
        "type": "bottom",
    }


def check_top_signal(df: pd.DataFrame) -> dict:
    """顶部信号: 60日新高 + 3日跌>3% + 融资>90% (预警)
    确认: 预警 + QVIX 3日涨>0%"""
    latest = df.iloc[-1]
    is_high = bool(latest["is_high"]) if pd.notna(latest["is_high"]) else False

    # 新高后 3 日内跌幅（需要看 df 尾部 4 行）
    drop3 = float("nan")
    if is_high:
        base = float(latest["spot"])
        tail = df.tail(4)  # 含当天在内 4 行
        if len(tail) >= 4:
            window = tail.iloc[1:]  # 之后 3 天
            closes = window["spot"].dropna().values
            if len(closes) > 0:
                drop3 = (min(closes) / base - 1) * 100

    # 注意：这几个标志必须转成 Python bool —— pandas/numpy 的 bool_ 不是
    # JSON 可序列化对象，会让 write_status() 的 json.dumps 直接炸
    # （2026-10-24 单元测试撞出来的隐患：is_high=True 时心跳路径必崩）。
    drop_ok = bool(pd.notna(drop3) and drop3 < -3.0)
    margin_ok = bool(pd.notna(latest["margin_pct"]) and float(latest["margin_pct"]) > 0.90)
    qvix_rise_ok = bool(pd.notna(latest["qvix_mom_3d"]) and float(latest["qvix_mom_3d"]) > 0)

    warn = bool(is_high and drop_ok and margin_ok)
    confirm = bool(warn and qvix_rise_ok)
    qvix_ref3, qvix_ref3_date, qvix_ref3_lag = _qvix_reference(df, "qvix_mom_3d")

    return {
        "date": latest["date"].strftime("%Y-%m-%d"),
        "spot_price": float(latest["spot"]),
        "fut_price": float(latest["fut"]) if pd.notna(latest.get("fut")) else float(latest["spot"]),
        "rsi14": float(latest["rsi14"]),
        "qvix": float(latest["qvix"]),
        "qvix_mom_3d": float(latest["qvix_mom_3d"]) if pd.notna(latest["qvix_mom_3d"]) else float("nan"),
        "qvix_mom_3d_ref": qvix_ref3,
        "qvix_ref_date": qvix_ref3_date,
        "qvix_ref3_lag": qvix_ref3_lag,
        "vol_60d": float(latest["vol_60d"]),
        "margin_pct": float(latest["margin_pct"]) if pd.notna(latest["margin_pct"]) else float("nan"),
        "drop3": drop3,
        "is_high": is_high,
        "drop_ok": drop_ok,
        "margin_ok": margin_ok,
        "qvix_rise_ok": qvix_rise_ok,
        "triggered": warn,           # 预警触发
        "confirmed": confirm,        # 强信号
        "type": "top_warn" if warn else "top",
    }


def _status_icon(ok: bool) -> str:
    """状态图标"""
    return "✅" if ok else "❌"


def _status_icon3(ok, has_data: bool = True) -> str:
    """三态图标：满足 ✅ / 不满足 ❌ / **数据缺失 ⚠️（不是"不满足"）**。

    为什么要有第三态：守门触发时（某项数据缺），那条 ❌ 会被读成
    「条件没满足」，而真实含义是「没数据可判」。2026-10-25 用户就指着
    「融资20日 -7.25% ❌ < 0」问「为什么 -7.25<0 显示不满足」。
    """
    if not has_data:
        return "⚠️缺"
    return "✅" if ok else "❌"


def bottom_state_text(b: dict) -> str:
    """抄底状态文案（2026-11-09 起："有一项缺数据"不再等于"整轮拒判"）。"""
    if b.get("hard_bad"):
        return "⚠️ 拒判（数据不齐）"
    if b.get("triggered"):
        return "🚨 触发"
    if b.get("pending_confirm"):
        miss = "QVIX" if not b.get("qvix_has_data", True) else "融资"
        return f"⚠️ 待确认（{miss} 缺，其余条件已满足）"
    if not b.get("qvix_has_data", True):
        return "未触发（QVIX 缺，条件2 无法确认）"
    if not b.get("margin_has_data", True):
        return "未触发（融资缺，条件4 无法确认）"
    return "未触发"


def top_state_text(t: dict) -> str:
    """顶部状态文案：QVIX 只影响「确认」，缺了照样能判「预警」。"""
    if t.get("confirmed"):
        return "🔴 确认"
    if t.get("triggered"):
        return ("⚠️ 预警（QVIX 3日缺，未确认）"
                if pd.isna(t.get("qvix_mom_3d", float("nan"))) else "🟠 预警")
    if pd.isna(t.get("qvix_mom_3d", float("nan"))):
        return "未触发（QVIX 缺，确认条件无法判定）"
    return "未触发"


def data_gap_note(b: dict) -> str:
    """缺数据时的说明行：**具体缺哪几天**、影响哪条条件、何时自动恢复。"""
    if b.get("hard_bad"):
        return (f"\n> ⚠️ **{b.get('stale_reason', '数据滞后')}**: 本轮信号拒判，"
                f"数据补齐后自动恢复")
    if not b.get("qvix_has_data", True):
        missed = b.get("missing_qvix_dates") or []
        txt = "、".join(missed) if missed else "最新交易日"
        return (f"\n> ⚠️ **QVIX 缺 {txt}**（optbbs 源站当日没发布官方值）→ 5 日动量算不出来，"
                f"抄底条件2 无法确认；其余条件照常判定（09:20 补判会重判，真信号第一时间推送）")
    if not b.get("margin_has_data", True):
        return (f"\n> ⚠️ **融资余额缺最新日**（上交所数据未更新）→ 抄底条件4 无法确认；"
                f"其余条件照常判定")
    return ""


def build_bottom_message(r: dict, last_date: str, note: str = "") -> str:
    """抄底信号消息"""
    level, why = signal_strength(r)
    lines = [
        "## 🚨 抄底信号触发",
        f"**日期**: {r['date']}　上次: {last_date}",
        f"**中证500**: {r['spot_price']:.1f}　**IC期货**: {r['fut_price']:.1f}",
        "---",
        "**四条件检查**",
        f"1. RSI(14) = {r['rsi14']:.1f} {_status_icon(r['rsi_ok'])} 阈值 < 44",
        f"2. QVIX 5日 = {r['qvix_mom_5d']:.2%} {_status_icon(r['qvix_ok'])} 阈值 < -3%",
        f"3. 60日波动率 = {r['vol_60d']:.1f}% {_status_icon(r['vol_ok'])} 阈值 > 20%",
        f"4. 融资20日 = {r['margin_mom20']:.2%} {_status_icon(r['margin_ok'])} 阈值 < 0",
        "---",
        f"**信号强度**: {level}｜{why}",
        "**信号含义**: 恐慌释放 + 杠杆出清，属于底部特征区",
        "**历史参考**: 新口径 8 簇样本(2022-2026)｜20日 7/7、60日 6/6、**5日仅 4/7**"
        "（信号后首个可交易日开盘入场）→ 触发后仍可能继续下探，兑现窗口在 20 个交易日",
    ]
    if level == "弱":
        lines.append("**注意**: 弱信号仅作观察，不建议单独作为动作依据")
    if note:
        lines.append(f"**备注**: {note}")
    return "\n".join(lines)


def build_top_message(r: dict, last_date: str, note: str = "") -> str:
    """顶部信号消息（预警/确认）"""
    if r["confirmed"]:
        title = "## 🔴 顶部确认信号（强）"
        tail = ("**信号含义**: 强确认（历史参考：5 次，后续 5-20 日跌>5% 的比例 100%，"
                "样本 n=5，仅供参考）")
    else:
        title = "## 🟠 顶部预警信号"
        tail = ("**信号含义**: 预警（历史参考：12 次，后续 5-20 日跌>5% 的比例 83.3%，"
                "样本 n=12，仅供参考）")
    drop_str = f"{r['drop3']:.1f}%" if pd.notna(r['drop3']) else "nan"
    lines = [
        title,
        f"**日期**: {r['date']}　上次: {last_date}",
        f"**中证500**: {r['spot_price']:.1f}　**IC期货**: {r['fut_price']:.1f}",
        "---",
        "**三条件检查**",
        f"1. 60日新高 = {'✅' if r['is_high'] else '❌'} 需创新高",
        f"2. 新高后3日跌幅 = {drop_str} {_status_icon(r['drop_ok'])} 阈值 < -3%",
        f"3. 融资分位 = {r['margin_pct']:.2f} {_status_icon(r['margin_ok'])} 阈值 > 90%",
        f"4. QVIX 3日 = {r['qvix_mom_3d']:.2%} {_status_icon(r['qvix_rise_ok'])} 阈值 > 0%（确认用）",
        "---",
        tail,
    ]
    if note:
        lines.append(f"**备注**: {note}")
    return "\n".join(lines)


def write_alert(result: dict, last_date: str, note: str = ""):
    """触发时：写最新信号 + 更新历史 + 追加日志"""
    if result["type"] == "bottom":
        message = build_bottom_message(result, last_date, note)
    else:
        message = build_top_message(result, last_date, note)

    # 更新历史
    history = load_history()
    history.append({
        "date": result["date"],
        "type": result["type"],
        "fut_price": result["fut_price"],
        "rsi14": result["rsi14"],
        "qvix": result["qvix"],
        "qvix_mom_5d": result.get("qvix_mom_5d"),
        "qvix_mom_3d": result.get("qvix_mom_3d"),
        "vol_60d": result["vol_60d"],
        "margin_mom20": result.get("margin_mom20"),
        "margin_pct": result.get("margin_pct"),
        "drop3": result.get("drop3"),
        "confirmed": result.get("confirmed", False),
        "last_trigger": last_date,
    })
    save_history(history)

    # 写 latest_signal.json（给 LLM 读取）
    payload = {
        **result,
        "last_trigger": last_date,
        "message": message,
    }
    LATEST_SIGNAL.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    logging.info(f"信号触发！写入 {LATEST_SIGNAL}, 历史={len(history)} 条")
    return payload


def write_status(bottom: dict, top: dict):
    """每日状态：无论触发与否，都写 latest_signal.json（每日心跳）"""
    b_state = bottom_state_text(bottom)
    t_state = top_state_text(top)
    qvix_mark = "✓" if bottom['qvix_ok'] else ("⚠️缺" if not bottom.get('qvix_has_data', True) else "✗")
    margin_mark = "✓" if bottom['margin_ok'] else ("⚠️缺" if not bottom.get('margin_has_data', True) else "✗")
    summary = (
        f"日期: {bottom['date']}\n"
        f"中证500: {bottom['spot_price']:.1f}  IC期货: {bottom['fut_price']:.1f}\n"
        f"--- 抄底 ---\n"
        f"RSI(14): {bottom['rsi14']:.1f}  {'✓' if bottom['rsi_ok'] else '✗'} (< 44)\n"
        f"QVIX 5日: {bottom['qvix_mom_5d']:.2%}  {qvix_mark} (< -3%)\n"
        f"60日vol: {bottom['vol_60d']:.1f}%  {'✓' if bottom['vol_ok'] else '✗'} (> 20%)\n"
        f"融资20日: {bottom['margin_mom20']:.2%}  {margin_mark} (< 0)\n"
        f"--- 顶部 ---\n"
        f"60日新高: {'✓' if top['is_high'] else '✗'}  新高后3日: {top['drop3']:.1f}%\n"
        f"融资分位: {top['margin_pct']:.2f}   QVIX 3日: {top['qvix_mom_3d']:.2%}\n"
        f"状态: 抄底{b_state} | 顶部{t_state}"
    )

    payload = {
        **bottom,
        "top": {k: top[k] for k in ["triggered", "confirmed", "drop3", "margin_pct",
                                    "qvix_mom_3d", "is_high", "drop_ok", "margin_ok", "qvix_rise_ok"]},
        "type": "heartbeat",
        "summary": summary,
    }
    LATEST_SIGNAL.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    logging.info(f"心跳更新 {LATEST_SIGNAL}: 抄底{'触发' if bottom['triggered'] else '未触发'} 顶部{'预警' if top['triggered'] else '未触发'}")


def notify_dingtalk(result: dict, last_date: str = None, note: str = ""):
    """通过钉钉发送通知"""
    if result["type"] == "bottom":
        msg = build_bottom_message(result, last_date, note)
        title = "🚨 抄底信号触发"
    elif result["confirmed"]:
        msg = build_top_message(result, last_date, note)
        title = "🔴 顶部确认信号触发"
    else:
        msg = build_top_message(result, last_date, note)
        title = "🟠 顶部预警信号触发"

    try:
        if push_bot_message(msg, title=title):
            logging.info(f"钉钉通知已发送（{result['date']} {title}）")
        else:
            logging.error(f"钉钉通知发送失败（{result['date']} {title}）")
    except Exception as e:
        logging.error(f"钉钉通知失败: {e}")


def notify_dingtalk_heartbeat(bottom: dict, top: dict, weekend: bool = False):
    """每日心跳钉钉通知（weekend=True 时是「非交易日回顾」）"""
    # nan 指标显示成「缺」而不是 "nan%" —— 缺数据不是"指标为 0"
    def _pct(v) -> str:
        return f"{v:.2%}" if pd.notna(v) else "缺"

    def _num(v, fmt: str) -> str:
        return format(v, fmt) if pd.notna(v) else "缺"

    # 底部状态（三态：✅满足 / ❌不满足 / ⚠️缺=没数据可判）
    def _qvix_show(mom, ref_mom, ref_date, ref_lag) -> str:
        """最新交易日缺 QVIX 时，用「上一个有效日」的动量填空并标明口径。

        2026-10-26：optbbs 源站 10-23/10-24 连续没回填，卡片原来只显示一个「缺」，
        用户根本看不出是源站停更还是自己这边坏了。现在把最后能算的动量摆出来，
        标「截至 X 日、滞后 N 日、仅参考」—— **数字给到，判定口径不变**。
        """
        if pd.notna(mom):
            return f"{mom:.2%}"
        if ref_mom is not None and pd.notna(ref_mom):
            d = (ref_date or "")[5:]
            return f"{ref_mom:.2%}（截至 {d}，滞后{ref_lag}日，仅参考）"
        return "缺"

    b_items = [
        f"RSI(14) = {bottom['rsi14']:.1f} {_status_icon3(bottom['rsi_ok'])} < 44",
        f"QVIX 5日 = {_qvix_show(bottom['qvix_mom_5d'], bottom.get('qvix_mom_5d_ref'), bottom.get('qvix_ref_date'), bottom.get('qvix_ref_lag'))} "
        f"{_status_icon3(bottom['qvix_ok'], bottom.get('qvix_has_data', True))} < -3%",
        f"60日波动率 = {_num(bottom['vol_60d'], '.1f')}% {_status_icon3(bottom['vol_ok'])} > 20%",
        f"融资20日 = {_pct(bottom['margin_mom20'])} "
        f"{_status_icon3(bottom['margin_ok'], bottom.get('margin_has_data', True))} < 0",
    ]
    # 顶部状态
    drop_str = f"{top['drop3']:.1f}%" if pd.notna(top['drop3']) else "--"
    t_items = [
        f"60日新高 = {'✅ 是' if top['is_high'] else '❌ 否'}",
        f"新高后3日跌幅 = {drop_str} "
        f"{_status_icon3(top['drop_ok'], pd.notna(top['drop3']))} < -3%",
        f"融资分位 = {_num(top['margin_pct'], '.2f')} "
        f"{_status_icon3(top['margin_ok'], pd.notna(top['margin_pct']))} > 90%",
        f"QVIX 3日 = {_qvix_show(top['qvix_mom_3d'], top.get('qvix_mom_3d_ref'), top.get('qvix_ref_date'), top.get('qvix_ref3_lag'))} "
        f"{_status_icon3(top['qvix_rise_ok'], pd.notna(top['qvix_mom_3d']))} > 0%",
    ]
    # 状态行
    b_status = bottom_state_text(bottom)
    t_status = top_state_text(top)

    # 缺失说明（具体缺哪天 / 影响哪条条件 / 何时恢复）
    stale_note = data_gap_note(bottom)

    # 行情源滞后提示（结论基于哪个收盘日，明说）
    lag_note = data_lag_note(bottom["date"])

    # 自算兜底提示（只在这一栏出现，判定口径完全不受影响）
    self_note = ""
    sv = bottom.get("qvix_self_val")
    sm = bottom.get("qvix_self_mom_5d")
    if sv is not None and pd.notna(sv):
        mom_txt = f"，5日 {sm:+.2%}" if (sm is not None and pd.notna(sm)) else ""
        self_note = (f"\n> 🧮 官方 QVIX 未回填，这里是**自算**（{bottom['date']} = {sv:.1f}{mom_txt}）："
                     f"用上交所官方 IV 按 CBOE 方差互换还原，实测水位偏差 ~4%、5 日动量相关 0.83、"
                     f"阈值一致 81% —— **仅参考，不参与判定**")

    lines = [
        "## 📊 IC 非交易日回顾" if weekend else "## 📊 IC 监控日报",
        f"**日期**: {bottom['date']}" + ("（最新交易日）" if weekend else ""),
        f"**中证500**: {bottom['spot_price']:.1f}　**IC期货**: {bottom['fut_price']:.1f}",
        "---",
        "### 🟢 抄底信号（做多）",
        *[f"- {x}" for x in b_items],
        f"> 状态: **{b_status}**",
        "---",
        "### 🔴 顶部信号（做空）",
        *[f"- {x}" for x in t_items],
        f"> 状态: **{t_status}**",
    ]
    if stale_note:
        lines.append(stale_note)
    if self_note:
        lines.append(self_note)
    if lag_note:
        lines.append(lag_note)
    if weekend:
        lines.append("> 周末无新交易数据，以上是**最新交易日**的判定结果；"
                     "开盘前如有新信号会第一时间推送。")
    try:
        if weekend:
            title = "IC 非交易日回顾"
        elif bottom.get("pending_confirm"):
            title = "IC 待确认信号"      # 其余条件已满足、只差缺数据那项 → 值得单独提示
        else:
            title = "IC 信号心跳播报"
        if push_bot_message("\n".join(lines), title=title):
            logging.info(f"钉钉心跳已发送（{bottom['date']}{'（周末回顾）' if weekend else ''}）")
        else:
            logging.error(f"钉钉心跳发送失败（{bottom['date']}）")
    except Exception as e:
        logging.error(f"钉钉心跳失败: {e}")


def notify_recheck(result: dict, days_ago: int):
    """事后修正信号触发通知（用于前几天数据被修正后补抓）"""
    lines = [
        "## 🔔 事后修正触发",
        f"**原始日期**: {result['date']}（{days_ago} 天前）",
        "**说明**: 该日数据经过数日修正后，满足触发条件",
        f"**中证500**: {result['spot_price']:.1f}",
        "**注意**: 事后修正信号，当日实时未抓取，仅作历史回溯参考",
    ]
    try:
        if push_bot_message("\n".join(lines), title="IC 事后修正触发"):
            logging.info(f"事后修正触发已通知: {result['date']} ({days_ago}天前)")
        else:
            logging.error(f"事后修正触发通知失败: {result['date']}")
    except Exception as e:
        logging.error(f"事后修正通知失败: {e}")


def wait_until(target_hour: int, target_minute: int):
    """等到指定时间"""
    while True:
        now = datetime.now()
        if now.hour == target_hour and now.minute >= target_minute:
            return
        if now.hour > target_hour or (now.hour == target_hour and now.minute > target_minute + 1):
            return
        time.sleep(20)


def is_weekday():
    return datetime.now().weekday() < 5


def load_state() -> dict:
    """读取每日去重状态（--once 模式用）"""
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(**kw) -> dict:
    state = load_state()
    state.update(kw)
    try:
        STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logging.warning(f"写状态文件失败: {e}")
    return state


def run_once(force: bool = False, dry_run: bool = False, heartbeat: bool = True) -> dict:
    """跑一次检查就退出（机器人 08:50 自动调度用）。

    与 main() 的常驻循环等价，但：
    - 不等待、不循环，算完就退（由调用方保证只在交易日 08:50 触发一次）；
    - 同一天的心跳/信号用状态文件 + 历史文件去重，重启不会重复刷屏；
    - dry_run 只打印不发送、不写任何状态，用于验证。
    """
    now = datetime.now()
    if not dry_run:
        save_state(
            last_attempt=now.strftime("%Y-%m-%d %H:%M:%S"),
            last_attempt_day=now.strftime("%Y-%m-%d"),
        )

    # 周末也跑（2026-10-24 用户要求）：akshare 的「最新数据」在周六早上 = **周五收盘**
    # （akshare 当天数据收盘后 3-5 小时才更新），所以周末这一轮是**第一次**判到周五
    # 收盘数据，有实际价值。信号去重照旧（按数据日），周五推过的信号周末不会重推。
    weekend = now.weekday() >= 5

    df = prepare_signals(load_latest_data())
    bottom = check_bottom_signal(df)
    top = check_top_signal(df)
    logging.info(
        f"日期: {bottom['date']}, "
        f"抄底: RSI={bottom['rsi14']:.1f}({'Y' if bottom['rsi_ok'] else 'N'}) "
        f"QVIX5d={bottom['qvix_mom_5d']:.2%}({'Y' if bottom['qvix_ok'] else 'N'}) "
        f"vol={bottom['vol_60d']:.1f}%({'Y' if bottom['vol_ok'] else 'N'}) "
        f"融20d={bottom['margin_mom20']:.2%}({'Y' if bottom['margin_ok'] else 'N'}) "
        f"→ 触发: {bottom['triggered']} | "
        f"顶部: 新高={'Y' if top['is_high'] else 'N'} "
        f"跌3日={top['drop3']:.1f}% 融分位={top['margin_pct']:.2f} "
        f"QVIX3d={top['qvix_mom_3d']:.2%} → 预警:{top['triggered']} 确认:{top['confirmed']}"
    )

    result = {
        "date": bottom["date"],
        "bottom_triggered": bottom["triggered"],
        "top_triggered": top["triggered"],
        "top_confirmed": top["confirmed"],
        "data_ok": bottom.get("data_ok", True),
        "hard_bad": bool(bottom.get("hard_bad", False)),
        "pending_confirm": bool(bottom.get("pending_confirm", False)),
        "missing_qvix_dates": bottom.get("missing_qvix_dates") or [],
        "sent": [],
    }

    # ---------------------------------------------------- 规则 2：长假前夜顺延
    # 最新数据日后面紧跟 >=3 天连续休市 → 本轮不判定，等节后第一个交易日重判。
    # （2026-09-24 中秋前那次假信号就是被这条拦掉的：节后 09-28~10-09 连续 5 日都不成立）
    data_date = df["date"].iloc[-1]
    state = load_state()
    deferred_date = state.get("deferred_date")
    if not force and is_pre_long_holiday(data_date):
        write_status(bottom, top)
        if not dry_run:
            save_state(
                deferred_date=data_date.strftime("%Y-%m-%d"),
                deferred_at=now.strftime("%Y-%m-%d %H:%M:%S"),
            )
        logging.info(
            "最新数据日 %s 后面紧跟长假（下一个交易日相距 >=%d 天），"
            "本轮不判定，顺延到节后第一个交易日重判",
            data_date.date(), LONG_HOLIDAY_GAP_DAYS,
        )
        result["skipped"] = "pre-holiday-deferred"
        result["deferred_date"] = data_date.strftime("%Y-%m-%d")
        return result

    # 上一轮有没有被顺延的节前触发？本轮判定用的就是节后第一个交易日的数据 → 带上备注
    note = ""
    if deferred_date and deferred_date != bottom["date"]:
        note = f"{deferred_date} 为节前触发，已顺延至 {bottom['date']} 重判"
        logging.info(note)
    if deferred_date and not dry_run:
        save_state(deferred_date=None, deferred_at=None)

    # 周一周报由调度器单独排（scheduler 里的 ic_weekly 作业），这里不再重复触发

    # 今日是否已有信号落地（含上一条进程/今早推过的）
    bottom_done = already_alerted(bottom["date"], "bottom")
    top_done = already_alerted(top["date"], top["type"])

    # 抄底信号
    if bottom["triggered"] and (force or not bottom_done):
        last_date = get_last_trigger_date(bottom["date"], "bottom")
        if dry_run:
            logging.info("[dry-run] 抄底信号:\n%s",
                         build_bottom_message(bottom, last_date, note))
            result["sent"].append("bottom(dry-run)")
        else:
            payload = write_alert(bottom, last_date, note)
            notify_dingtalk(bottom, last_date, note)
            emit_signal(payload)
            save_state(last_push_data_date=bottom["date"],
                       last_signal_push_day=now.strftime("%Y-%m-%d"))
            result["sent"].append("bottom")
    # 顶部信号（预警/确认）
    elif top["triggered"] and (force or not top_done):
        last_date = get_last_trigger_date(top["date"], "top_warn")
        if dry_run:
            logging.info("[dry-run] 顶部信号:\n%s",
                         build_top_message(top, last_date, note))
            result["sent"].append(f"{top['type']}(dry-run)")
        else:
            payload = write_alert(top, last_date, note)
            notify_dingtalk(top, last_date, note)
            emit_signal(payload)
            save_state(last_push_data_date=top["date"],
                       last_signal_push_day=now.strftime("%Y-%m-%d"))
            result["sent"].append(top["type"])

    # 没发信号 → 每日心跳：**按运行日去重**（每天早上必有一条），不按数据日去重。
    # 2026-10-25 改：老逻辑有两条会把整轮吞掉的分支 ——
    #   ① 「数据日没变就跳过」→ 周六推过 10-23，周日 08:50 判 no-new-data → 静默；
    #   ② 「触发过就不补心跳」→ 信号是**昨天**按同一数据日推的，今天也静默。
    # 现在：信号仍按**数据日**去重（already_alerted，同一条信号不重推），
    # 心跳按**运行日**去重 → 每个早上必有一条结论（含"信号此前已触发"的状态）。
    # 同一天里信号已经推过 → 不再补日报（同一天不刷两条）。
    if not result["sent"] and heartbeat:
        today = now.strftime("%Y-%m-%d")
        st = load_state()
        if st.get("last_signal_push_day") == today and not force:
            logging.info(f"{today} 已推过信号，不再补发心跳")
            result["skipped"] = "signal-already-sent"
        elif st.get("heartbeat_day") == today and not force:
            logging.info(f"{today} 心跳已发过，跳过")
            result["skipped"] = "heartbeat-already-sent"
        elif dry_run:
            logging.info("[dry-run] 心跳: %s 抄底未触发 / 顶部未触发", bottom["date"])
            result["sent"].append("heartbeat(dry-run)")
        else:
            if bottom["triggered"] or top["triggered"]:
                logging.info(
                    "信号此前已按数据日 %s 推送过，今天补一条日报说明当前状态",
                    bottom["date"])
            write_status(bottom, top)
            notify_dingtalk_heartbeat(bottom, top, weekend=weekend)
            save_state(heartbeat_day=today, last_push_data_date=bottom["date"])
            result["sent"].append("heartbeat")

    if not dry_run:
        save_state(last_result=result, last_run_day=now.strftime("%Y-%m-%d"),
                   last_judged_date=bottom["date"])
    logging.info(f"--once 结束: sent={result['sent'] or '无'}")
    return result


def main():
    logging.info("=" * 60)
    logging.info("IC 期货多信号监控启动")
    logging.info("抄底: RSI<44 + QVIX5d<-3% + vol>20% + 融资20日降")
    logging.info("顶部预警: 新高 + 3日跌3% + 融资>90%")
    logging.info("顶部确认: 预警 + QVIX3日涨>0%")
    logging.info("=" * 60)

    if not is_weekday():
        logging.info("今天不是工作日，常驻模式退出"
                     "（注意：调度器用的 --once 模式周末会跑「非交易日回顾」）")
        return

    # 等到次日 8:50（开盘前预警，akshare 前一天数据已更新）
    logging.info("等待到次日 8:50...")
    wait_until(8, 50)

    # 8:50 之后开始检查
    status_written = False  # 每日心跳只写一次
    weekly_sent = False     # 周报每周一 8:50 发一次
    while True:
        now = datetime.now()
        if now.hour >= 10:
            logging.info("已过 10:00，结束早间监控")
            break

        try:
            df = load_latest_data()
            df = prepare_signals(df)
            bottom = check_bottom_signal(df)
            top = check_top_signal(df)

            # 周一 8:50: 发送全历史触发明细周报（幂等, 当日PNG已存在则跳过）
            if not weekly_sent and now.weekday() == 0:
                try:
                    from ic_weekly_report import generate_and_send
                    png = generate_and_send()
                    logging.info(f"周一周报已处理: {png}")
                except Exception as e:
                    logging.error(f"周报生成/发送失败: {e}")
                weekly_sent = True

            logging.info(
                f"日期: {bottom['date']}, "
                f"抄底: RSI={bottom['rsi14']:.1f}({'Y' if bottom['rsi_ok'] else 'N'}) "
                f"QVIX5d={bottom['qvix_mom_5d']:.2%}({'Y' if bottom['qvix_ok'] else 'N'}) "
                f"vol={bottom['vol_60d']:.1f}%({'Y' if bottom['vol_ok'] else 'N'}) "
                f"融20d={bottom['margin_mom20']:.2%}({'Y' if bottom['margin_ok'] else 'N'}) "
                f"→ 触发: {bottom['triggered']} | "
                f"顶部: 新高={'Y' if top['is_high'] else 'N'} "
                f"跌3日={top['drop3']:.1f}% 融分位={top['margin_pct']:.2f} "
                f"QVIX3d={top['qvix_mom_3d']:.2%} → 预警:{top['triggered']} 确认:{top['confirmed']}"
            )

            # 规则 2：长假前夜 → 本轮不判定，等节后第一个交易日重判
            if is_pre_long_holiday(df["date"].iloc[-1]):
                logging.info("最新数据日 %s 是长假前夜，本轮不判定（顺延到节后重判）",
                             df["date"].iloc[-1].date())
                time.sleep(600)
                continue

            # 触发处理
            triggered_any = False
            if bottom["triggered"] and not already_alerted(bottom["date"], "bottom"):
                last_date = get_last_trigger_date(bottom["date"], "bottom")
                payload = write_alert(bottom, last_date)
                logging.info(f"🚨 抄底触发！{payload['message']}")
                notify_dingtalk(bottom, last_date)
                emit_signal(payload)
                triggered_any = True

            if top["triggered"] and not already_alerted(top["date"], top["type"]):
                last_date = get_last_trigger_date(top["date"], "top_warn")
                payload = write_alert(top, last_date)
                logging.info(f"{'🔴' if top['confirmed'] else '⚠️'} 顶部触发！{payload['message']}")
                notify_dingtalk(top, last_date)
                emit_signal(payload)
                triggered_any = True

            if not triggered_any and not status_written:
                # 首次成功拉取，写每日心跳 + 钉钉
                write_status(bottom, top)
                notify_dingtalk_heartbeat(bottom, top)
                status_written = True
                logging.info(f"已写每日心跳（{bottom['date']} 未触发）")

        except Exception as e:
            logging.error(f"检查失败: {e}")

        time.sleep(600)  # 每 10 分钟重试

    logging.info("监控结束（10:00 退出）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="IC 期货多信号监控（抄底 + 顶部）")
    ap.add_argument("--once", action="store_true",
                    help="只跑一次检查就退出（机器人 08:50 自动调度用）")
    ap.add_argument("--force", action="store_true",
                    help="忽略当日去重/无新数据限制，强制跑并重发")
    ap.add_argument("--dry-run", action="store_true",
                    help="只计算并打印，不推送、不写状态（验证用）")
    ap.add_argument("--triggers-only", action="store_true",
                    help="未触发时不发每日心跳日报")
    args = ap.parse_args()

    if args.once:
        res = run_once(force=args.force, dry_run=args.dry_run,
                       heartbeat=not args.triggers_only)
        print(json.dumps(res, ensure_ascii=True))
    else:
        main()
