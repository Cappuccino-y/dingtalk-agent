"""
IC 期货多信号监控（抄底 + 顶部预警/确认）
- 次日 8:50 检测中证500指数
- IC 期货价格仅做参考，避免合约换月导致指标失真
- 触发写入日志 + latest_signal.json + signal_history.json
- 钉钉通知 + autoworkflow 可捕获 stdout

信号体系（全部经 2015-2026 回测验证）:

【抄底信号】RSI<44 + QVIX 5d<-3% + vol>20% + 融资20日降
  历史: 9 簇射/3.9年, 已验证 7/7 = 100% 60日胜率
  → 恐慌释放 + 杠杆出清 → 做多

【顶部预警】60日新高 + 3日跌>3% + 融资>90%
  历史: 12次, 83.3% 后续 5-20 日跌>5%
  → 高位确认 → 做空预警

【顶部确认】顶部预警 + QVIX 3日涨>0%
  历史: 5次, 100% 后续 5-20 日跌>5%
  → 恐慌加剧 → 做空确认

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
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd
import numpy as np

# 本仓库根目录（dingtalk-agent）。作业脚本放在 jobs/ 下，所以上两级就是仓库根。
HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for _p in (REPO, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# 钉钉通知
from tools.notifycation import send_dingtalk_message

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
    qvix = fetch_qvix("500etf")
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


def prepare_signals(df: pd.DataFrame) -> pd.DataFrame:
    """计算所有指标列"""
    df = df.copy()
    df["rsi14"] = compute_rsi14(df["spot"], 14)
    # fill_method=None: 数据源滞后产生NaN时动量=NaN→条件不满足
    # (默认pad会用旧值填充NaN, 用陈旧数据算出假动量, 2026-10-12假触发根因)
    df["qvix_mom_3d"] = df["qvix"].pct_change(3, fill_method=None)
    df["qvix_mom_5d"] = df["qvix"].pct_change(5, fill_method=None)
    df["vol_60d"] = compute_vol_60d(df["spot"], 60)
    df["margin_mom20"] = df["margin"].pct_change(20, fill_method=None)
    df["margin_pct"] = df["margin"].rolling(250).apply(
        lambda x: (x.iloc[-1] >= x).mean(), raw=False)
    # 60 日新高（前一日为参考，今日创新高）
    df["high60_prev"] = df["spot"].rolling(60, min_periods=60).max().shift(1)
    df["is_high"] = df["spot"] > df["high60_prev"]
    return df


def check_bottom_signal(df: pd.DataFrame) -> dict:
    """抄底信号: RSI<44 + QVIX 5d<-3% + vol>20% + 融资20日降"""
    latest = df.iloc[-1]
    # 数据完整性守门: 任一关键数据缺失/滞后 → 拒判(2026-10-12假触发教训)
    stale = []
    if pd.isna(latest["qvix"]):
        stale.append("QVIX")
    if pd.isna(latest["margin_mom20"]):
        stale.append("融资余额")
    qvix_last_date = df.loc[df["qvix"].notna(), "date"].iloc[-1] if df["qvix"].notna().any() else None
    if qvix_last_date is not None and (latest["date"] - qvix_last_date).days >= 2:
        stale.append(f"QVIX滞后{(latest['date'] - qvix_last_date).days}天")
    margin_last_date = df.loc[df["margin"].notna(), "date"].iloc[-1] if df["margin"].notna().any() else None
    if margin_last_date is not None and (latest["date"] - margin_last_date).days >= 2:
        stale.append(f"融资滞后{(latest['date'] - margin_last_date).days}天")
    if stale:
        logging.warning(f"数据完整性守门触发({', '.join(stale)}), 本轮信号拒判")
    rsi_ok = float(latest["rsi14"]) < 44
    qvix_ok = (not stale) and pd.notna(latest["qvix_mom_5d"]) and float(latest["qvix_mom_5d"]) < -0.03
    vol_ok = float(latest["vol_60d"]) > 20
    margin_ok = (not stale) and pd.notna(latest["margin_mom20"]) and float(latest["margin_mom20"]) < 0
    stale_reason = "、".join(stale) if stale else ""
    return {
        "date": latest["date"].strftime("%Y-%m-%d"),
        "spot_price": float(latest["spot"]),
        "fut_price": float(latest["fut"]) if pd.notna(latest.get("fut")) else float(latest["spot"]),
        "rsi14": float(latest["rsi14"]),
        "qvix": float(latest["qvix"]),
        "qvix_mom_5d": float(latest["qvix_mom_5d"]) if pd.notna(latest["qvix_mom_5d"]) else float("nan"),
        "vol_60d": float(latest["vol_60d"]),
        "margin_mom20": float(latest["margin_mom20"]) if pd.notna(latest["margin_mom20"]) else float("nan"),
        "rsi_ok": bool(rsi_ok),
        "qvix_ok": bool(qvix_ok),
        "vol_ok": bool(vol_ok),
        "margin_ok": bool(margin_ok),
        "triggered": bool(rsi_ok and qvix_ok and vol_ok and margin_ok),
        "data_ok": not stale,
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

    drop_ok = pd.notna(drop3) and drop3 < -3.0
    margin_ok = pd.notna(latest["margin_pct"]) and float(latest["margin_pct"]) > 0.90
    qvix_rise_ok = pd.notna(latest["qvix_mom_3d"]) and float(latest["qvix_mom_3d"]) > 0

    warn = bool(is_high and drop_ok and margin_ok)
    confirm = bool(warn and qvix_rise_ok)

    return {
        "date": latest["date"].strftime("%Y-%m-%d"),
        "spot_price": float(latest["spot"]),
        "fut_price": float(latest["fut"]) if pd.notna(latest.get("fut")) else float(latest["spot"]),
        "rsi14": float(latest["rsi14"]),
        "qvix": float(latest["qvix"]),
        "qvix_mom_3d": float(latest["qvix_mom_3d"]) if pd.notna(latest["qvix_mom_3d"]) else float("nan"),
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


def build_bottom_message(r: dict, last_date: str) -> str:
    """抄底信号消息"""
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
        "**信号**: 恐慌释放 + 杠杆出清，疑似底部（历史 100% 60日胜率）",
        "**建议**: 加仓 1 手 IC 期货",
    ]
    return "\n".join(lines)


def build_top_message(r: dict, last_date: str) -> str:
    """顶部信号消息（预警/确认）"""
    if r["confirmed"]:
        title = "## 🔴 顶部确认信号（强）"
        tail = "**信号**: 强确认（历史 100% 后续 5-20 日跌>5%）\n**建议**: 考虑做空 1 手 IC 期货"
    else:
        title = "## 🟠 顶部预警信号"
        tail = "**信号**: 预警（历史 83.3% 后续 5-20 日跌>5%）\n**建议**: 减仓/观望，等待确认"
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
    return "\n".join(lines)


def write_alert(result: dict, last_date: str):
    """触发时：写最新信号 + 更新历史 + 追加日志"""
    if result["type"] == "bottom":
        message = build_bottom_message(result, last_date)
    else:
        message = build_top_message(result, last_date)

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
    summary = (
        f"日期: {bottom['date']}\n"
        f"中证500: {bottom['spot_price']:.1f}  IC期货: {bottom['fut_price']:.1f}\n"
        f"--- 抄底 ---\n"
        f"RSI(14): {bottom['rsi14']:.1f}  {'✓' if bottom['rsi_ok'] else '✗'} (< 44)\n"
        f"QVIX 5日: {bottom['qvix_mom_5d']:.2%}  {'✓' if bottom['qvix_ok'] else '✗'} (< -3%)\n"
        f"60日vol: {bottom['vol_60d']:.1f}%  {'✓' if bottom['vol_ok'] else '✗'} (> 20%)\n"
        f"融资20日: {bottom['margin_mom20']:.2%}  {'✓' if bottom['margin_ok'] else '✗'} (< 0)\n"
        f"--- 顶部 ---\n"
        f"60日新高: {'✓' if top['is_high'] else '✗'}  新高后3日: {top['drop3']:.1f}%\n"
        f"融资分位: {top['margin_pct']:.2f}   QVIX 3日: {top['qvix_mom_3d']:.2%}\n"
        f"状态: 抄底{'🚨' if bottom['triggered'] else '未触发'} | "
        f"顶部{'⚠️预警' if top['triggered'] else '未触发'}"
        f"{'🔴确认' if top['confirmed'] else ''}"
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


def notify_dingtalk(result: dict, last_date: str = None):
    """通过钉钉发送通知"""
    if result["type"] == "bottom":
        msg = build_bottom_message(result, last_date)
        title = "🚨 抄底信号触发"
    elif result["confirmed"]:
        msg = build_top_message(result, last_date)
        title = "🔴 顶部确认信号触发"
    else:
        msg = build_top_message(result, last_date)
        title = "🟠 顶部预警信号触发"

    try:
        send_dingtalk_message(msg)
        logging.info(f"钉钉通知已发送（{result['date']} {title}）")
    except Exception as e:
        logging.error(f"钉钉通知失败: {e}")


def notify_dingtalk_heartbeat(bottom: dict, top: dict):
    """每日心跳钉钉通知"""
    # 底部状态
    b_items = [
        f"RSI(14) = {bottom['rsi14']:.1f} {_status_icon(bottom['rsi_ok'])} < 44",
        f"QVIX 5日 = {bottom['qvix_mom_5d']:.2%} {_status_icon(bottom['qvix_ok'])} < -3%",
        f"60日波动率 = {bottom['vol_60d']:.1f}% {_status_icon(bottom['vol_ok'])} > 20%",
        f"融资20日 = {bottom['margin_mom20']:.2%} {_status_icon(bottom['margin_ok'])} < 0",
    ]
    # 顶部状态
    drop_str = f"{top['drop3']:.1f}%" if pd.notna(top['drop3']) else "--"
    t_items = [
        f"60日新高 = {'✅ 是' if top['is_high'] else '❌ 否'}",
        f"新高后3日跌幅 = {drop_str} {_status_icon(top['drop_ok'])} < -3%",
        f"融资分位 = {top['margin_pct']:.2f} {_status_icon(top['margin_ok'])} > 90%",
        f"QVIX 3日 = {top['qvix_mom_3d']:.2%} {_status_icon(top['qvix_rise_ok'])} > 0%",
    ]
    # 状态行
    b_status = "🚨 触发" if bottom['triggered'] else "未触发"
    t_status = "🔴 确认" if top['confirmed'] else ("🟠 预警" if top['triggered'] else "未触发")

    # 数据滞后提示
    stale_note = ""
    if not bottom.get("data_ok", True):
        stale_note = f"\n> ⚠️ **{bottom.get('stale_reason', '数据滞后')}**: 本轮信号拒判，数据补齐后自动恢复"

    lines = [
        "## 📊 IC 监控日报",
        f"**日期**: {bottom['date']}",
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
    try:
        send_dingtalk_message("\n".join(lines))
        logging.info(f"钉钉心跳已发送（{bottom['date']}）")
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
        send_dingtalk_message("\n".join(lines))
        logging.info(f"事后修正触发已通知: {result['date']} ({days_ago}天前)")
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

    if now.weekday() >= 5 and not force:
        logging.info("周末（%s），--once 跳过", now.strftime("%Y-%m-%d"))
        return {"skipped": "weekend"}

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
        "sent": [],
    }

    # 周一周报由调度器单独排（scheduler 里的 ic_weekly 作业），这里不再重复触发

    # 今日是否已有信号落地（含上一条进程/今早推过的）
    bottom_done = already_alerted(bottom["date"], "bottom")
    top_done = already_alerted(top["date"], top["type"])

    # 抄底信号
    if bottom["triggered"] and (force or not bottom_done):
        last_date = get_last_trigger_date(bottom["date"], "bottom")
        if dry_run:
            logging.info("[dry-run] 抄底信号:\n%s",
                         build_bottom_message(bottom, last_date))
            result["sent"].append("bottom(dry-run)")
        else:
            payload = write_alert(bottom, last_date)
            notify_dingtalk(bottom, last_date)
            print(f"SIGNAL_TRIGGERED:{json.dumps(payload, ensure_ascii=False)}")
            sys.stdout.flush()
            result["sent"].append("bottom")
    # 顶部信号（预警/确认）
    elif top["triggered"] and (force or not top_done):
        last_date = get_last_trigger_date(top["date"], "top_warn")
        if dry_run:
            logging.info("[dry-run] 顶部信号:\n%s",
                         build_top_message(top, last_date))
            result["sent"].append(f"{top['type']}(dry-run)")
        else:
            payload = write_alert(top, last_date)
            notify_dingtalk(top, last_date)
            print(f"SIGNAL_TRIGGERED:{json.dumps(payload, ensure_ascii=False)}")
            sys.stdout.flush()
            result["sent"].append(top["type"])

    # 没发信号 → 每日心跳（同一天只发一次；今天已有信号就不再补心跳）
    if not result["sent"] and heartbeat:
        today = now.strftime("%Y-%m-%d")
        if bottom["triggered"] or top["triggered"]:
            logging.info("今日信号已推送过（%s），不再补发心跳", bottom["date"])
            result["skipped"] = "signal-already-sent"
        elif (load_state().get("heartbeat_day") == today) and not force:
            logging.info(f"{today} 心跳已发过，跳过")
            result["skipped"] = "heartbeat-already-sent"
        elif dry_run:
            logging.info("[dry-run] 心跳: %s 抄底未触发 / 顶部未触发", bottom["date"])
            result["sent"].append("heartbeat(dry-run)")
        else:
            write_status(bottom, top)
            notify_dingtalk_heartbeat(bottom, top)
            save_state(heartbeat_day=today)
            result["sent"].append("heartbeat")

    if not dry_run:
        save_state(last_result=result, last_run_day=now.strftime("%Y-%m-%d"))
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
        logging.info("今天不是工作日，退出")
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

            # 触发处理
            triggered_any = False
            if bottom["triggered"] and not already_alerted(bottom["date"], "bottom"):
                last_date = get_last_trigger_date(bottom["date"], "bottom")
                payload = write_alert(bottom, last_date)
                logging.info(f"🚨 抄底触发！{payload['message']}")
                notify_dingtalk(bottom, last_date)
                print(f"SIGNAL_TRIGGERED:{json.dumps(payload, ensure_ascii=False)}")
                sys.stdout.flush()
                triggered_any = True

            if top["triggered"] and not already_alerted(top["date"], top["type"]):
                last_date = get_last_trigger_date(top["date"], "top_warn")
                payload = write_alert(top, last_date)
                logging.info(f"{'🔴' if top['confirmed'] else '⚠️'} 顶部触发！{payload['message']}")
                notify_dingtalk(top, last_date)
                print(f"SIGNAL_TRIGGERED:{json.dumps(payload, ensure_ascii=False)}")
                sys.stdout.flush()
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
                    help="忽略当日去重/周末限制，强制跑并重发")
    ap.add_argument("--dry-run", action="store_true",
                    help="只计算并打印，不推送、不写状态（验证用）")
    ap.add_argument("--triggers-only", action="store_true",
                    help="未触发时不发每日心跳日报")
    args = ap.parse_args()

    if args.once:
        res = run_once(force=args.force, dry_run=args.dry_run,
                       heartbeat=not args.triggers_only)
        print(json.dumps(res, ensure_ascii=False))
    else:
        main()
