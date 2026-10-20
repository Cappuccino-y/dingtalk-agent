"""每台机器**开机（登录）后立即**生成并推送一份「早报」——不设固定时刻，
因为开机时间本来就不固定。

和机器人（bot/listen.py）走**同一套架构**：
    agent（唯一大脑）──► market_wave / market_tech / market_overseas / market_news
    ──► 手机友好的早报 ──► 钉钉主动推送（markdown + 点位图）

刻意**不使用** datalayer/report_news_data.py 里那套旧的早报
（写死 prompt + 再调一次 LLM 的黑盒分析器），那是整改前的老路。

特点：
- **非交易日也发**，只是内容换成"回顾 + 下一交易日关注点"。
- 每次运行前把会话**重置回默认模型**（deepseek），只有当场额度耗尽才临时切兜底。
- 自己确保 OpenCode 后台服务在跑（开机时它常常还没起）。
- 距上次推送不足 `--min-interval` 分钟就跳过，避免反复重启刷屏。

用法：
    python jobs/premarket_push.py                # 手动跑一次
    python jobs/premarket_push.py --force        # 忽略去重强制再发
    python jobs/premarket_push.py --no-image     # 只推文字

定时由 scheduler 的 `premarket` 作业负责（每天 08:00，窗口 180 分钟）。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for _p in (REPO,):
    if _p not in sys.path:
        sys.path.insert(0, _p)

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stderr)
log = logging.getLogger("premarket")

BOT_DIR = os.path.join(REPO, ".opencode-bot")
# 会话文件必须和机器人（bot/listen.py 的 BRIDGE_DATA_DIR）指向同一个地方：
# 机器人每次启动都会删掉这个文件，好让盘前报告重新积累一份干净的上下文。
BRIDGE_DATA_DIR = os.path.join(REPO, "bot", "bridge_data")
SESSION_FILE = os.path.join(BRIDGE_DATA_DIR, "premarket_session.json")
PROMPT = (
    "盘前报告：现在是开盘前，请给出今天的中证1000操盘计划。\n"
    "务必先调 market_overseas 看隔夜外围、再调 market_wave 拿道氏三级别与波浪结构、"
    "再调 market_tech 拿技术面，最后按需调 market_news 补消息面。\n"
    "输出用手机卡片友好的写法（不要表格、不要 # 标题、不要代码围栏，重点用加粗和 > 引用），"
    "结构固定为：\n"
    "1. 隔夜外围与影响（美股方向、对中证1000的传导）\n"
    "2. 道氏三级别趋势 + 指数是否相互确认 + 波浪当前浪位/失效位\n"
    "3. 今日关键点位：支撑 / 压力 / 均线\n"
    "4. 今日方向判断与三档操作计划（入场区 / 止损 / 目标）\n"
    "5. 最重要的 1-2 条风险提示\n"
    "控制在 20 行内，直接给结论，不要客套。"
)
# 非交易日（周末/节假日）也推，但内容改成"回顾 + 下一交易日计划"
PROMPT_OFF = (
    "今天是休市日（A股不开盘）。请给出一份简短的早报：\n"
    "1. 回顾上一交易日中证1000的走势与道氏/波浪结构（可调 market_wave）\n"
    "2. 周末/假期的外围与消息面要点（可调 market_overseas / market_news，有则说，无则跳过）\n"
    "3. 下一个交易日的关注点与关键点位（支撑/压力/均线，可调 market_wave / market_tech）\n"
    "输出用手机卡片友好写法（不要表格、不要 # 标题、不要代码围栏，重点用加粗和 > 引用），"
    "控制在 12 行内，直接给结论，不要客套。"
)


# ------------------------------------------------------------------ OpenCode 服务
def _opencode_service_alive() -> bool:
    """后台服务是否在跑（读 service.json 里的 pid 并确认进程活着）。"""
    import ctypes
    svc = os.path.expanduser(r"~/.local/state/opencode/service.json")
    try:
        with open(svc, "r", encoding="utf-8") as fh:
            pid = int(json.load(fh).get("pid") or 0)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if not pid:
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(h)
    return bool(ok) and code.value == STILL_ACTIVE


def ensure_opencode_service(timeout: int = 90) -> bool:
    """确保 OpenCode 后台服务在跑。

    开机自启场景下它常常还没起来（它是随客户端按需启动的），
    而本脚本和机器人**都依赖它**。这里自己把它拉起来。
    """
    if _opencode_service_alive():
        log.info("OpenCode 服务：已在运行")
        return True

    exe = os.path.join(os.environ.get("APPDATA", ""), "npm", "node_modules",
                       "@opencode", "cli", "bin", "opencode.exe")
    if not os.path.exists(exe):
        log.error("找不到 opencode.exe：%s", exe)
        return False

    log.info("OpenCode 服务未运行，尝试启动…")
    DETACHED_PROCESS = 0x00000008
    try:
        # DEVNULL 三路都断，绝不让常驻进程继承调用方的管道句柄（opencode#32504）
        subprocess.Popen([exe, "serve", "--service"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=DETACHED_PROCESS)
    except Exception as exc:  # noqa: BLE001
        log.error("启动 OpenCode 服务失败: %s", exc)
        return False

    for _ in range(max(1, timeout // 3)):
        time.sleep(3)
        if _opencode_service_alive():
            log.info("OpenCode 服务：已就绪")
            return True
    log.error("OpenCode 服务启动超时（%ds）", timeout)
    return False


# ------------------------------------------------------------------ 交易日
def is_trading_day(day: datetime | None = None) -> bool:
    """是否是 A 股交易日（**只用于在报告里标注，不会因此跳过**）。

    优先用 akshare 的交易所日历（已验证可用，含节假日）；
    拿不到时退化成"工作日"近似并打日志。
    """
    day = day or datetime.now()
    try:
        import akshare as ak
        cal = ak.tool_trade_date_hist_sina()
        days = {str(d)[:10] for d in cal["trade_date"]}
        ok = day.strftime("%Y-%m-%d") in days
        log.info("交易日历(akshare): %s -> %s", day.strftime("%Y-%m-%d"),
                 "交易日" if ok else "非交易日")
        return ok
    except Exception as exc:  # noqa: BLE001
        wd = day.weekday() < 5
        log.warning("交易日历不可用（%s），退化为工作日判断 -> %s", exc,
                    "交易日" if wd else "非交易日")
        return wd


# ------------------------------------------------------------------ agent
def _ask_agent(trading: bool = True) -> str:
    """在机器人专属 Location 里跑一轮 agent，取最终文字回答。

    用**独立会话**（premarket_session.json），这样每天的早报自成一条线，
    不会和你在钉钉里手动聊天互相污染。
    """
    from tools.opencode_bridge import (
        DEFAULT_MODEL, FALLBACK_MODEL, OpenCodeService, apply_instructions,
        is_quota_error,
    )

    oc = OpenCodeService()
    try:
        oc.info()
    except Exception as exc:  # noqa: BLE001
        log.error("连不上本机 OpenCode 服务（%s）—— 先跑 `opencode service start`", exc)
        return ""

    sid = None
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as fh:
                sid = json.load(fh).get("session_id")
        except (OSError, json.JSONDecodeError):
            sid = None
    if sid:
        try:
            oc.messages(sid, limit=1)
        except Exception:  # noqa: BLE001
            log.info("旧盘前会话已失效，新建一个")
            sid = None
    if not sid:
        sess = oc.create_session(BOT_DIR, title="premarket-report",
                                 model=DEFAULT_MODEL)
        sid = sess["id"]
        os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
        with open(SESSION_FILE, "w", encoding="utf-8") as fh:
            json.dump({"session_id": sid}, fh, ensure_ascii=False)

    apply_instructions(oc, sid)
    # 每次运行前把会话**重置回默认模型**（deepseek）。
    # 否则上一次额度耗尽时切到兜底模型会被持久化下来，
    # 导致之后每天都在兜底模型上跑、再也不尝试主模型。
    try:
        oc.switch_model(sid, DEFAULT_MODEL["providerID"], DEFAULT_MODEL["id"])
        log.info("会话模型已重置为默认 %s/%s", DEFAULT_MODEL["providerID"],
                 DEFAULT_MODEL["id"])
    except Exception as exc:  # noqa: BLE001
        log.warning("重置默认模型失败（继续用当前模型）: %s", exc)
    log.info("盘前会话: %s", sid)

    def _run_once():
        prompt = PROMPT if trading else PROMPT_OFF
        oc.prompt(sid, prompt)
        done = threading.Event()

        def _waiter():
            try:
                oc.wait_idle(sid, timeout=600)
            finally:
                done.set()

        threading.Thread(target=_waiter, daemon=True).start()
        if not done.wait(timeout=660):
            log.error("等待 agent 超时")
            return None
        return oc.last_assistant(sid) or {}

    msg = _run_once()
    # 主模型额度耗尽 → 自动切兜底模型（和钉钉聊天同一套逻辑）
    if msg and is_quota_error(msg):
        log.warning("主模型额度/限流耗尽，切换兜底模型 %s",
                    FALLBACK_MODEL["id"])
        try:
            oc.switch_model(sid, FALLBACK_MODEL["providerID"],
                            FALLBACK_MODEL["id"])
        except Exception as exc:  # noqa: BLE001
            log.error("切换兜底模型失败: %s", exc)
            return ""
        msg = _run_once() or {}
        if is_quota_error(msg):
            log.error("兜底模型也失败，放弃本次盘前报告")
            return ""

    log.info("agent 完成 finish=%s model=%s", msg.get("finish"),
             (msg.get("model") or {}).get("id"))
    text = "".join(p.get("text") or "" for p in msg.get("content") or []
                   if p.get("type") == "text").strip()
    return text


# ------------------------------------------------------------------ 主流程
LAST_SENT_FILE = os.path.join(BRIDGE_DATA_DIR, "premarket_last_sent.json")


def _recently_sent(minutes: int) -> bool:
    """刚发过就跳过——开机/重启可能反复触发，避免刷屏。"""
    if minutes <= 0:
        return False
    try:
        with open(LAST_SENT_FILE, "r", encoding="utf-8") as fh:
            last = datetime.fromisoformat(json.load(fh).get("last_sent", ""))
        gap = (datetime.now() - last).total_seconds() / 60.0
        if gap < minutes:
            log.info("距上次推送仅 %.0f 分钟（<%d），跳过", gap, minutes)
            return True
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return False


def _mark_sent() -> None:
    try:
        os.makedirs(os.path.dirname(LAST_SENT_FILE), exist_ok=True)
        with open(LAST_SENT_FILE, "w", encoding="utf-8") as fh:
            json.dump({"last_sent": datetime.now().isoformat(timespec="seconds")},
                      fh, ensure_ascii=False)
    except OSError as exc:
        log.warning("写推送时间戳失败: %s", exc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略去重，强制再发一次")
    ap.add_argument("--min-interval", type=int, default=60,
                    help="距上次推送不足多少分钟就跳过（0=不去重，默认60）")
    ap.add_argument("--no-image", action="store_true", help="只推文字，不附图")
    a = ap.parse_args()

    now = datetime.now()
    log.info("=== 早报 %s ===", now.strftime("%Y-%m-%d %H:%M:%S"))

    if not a.force and _recently_sent(a.min_interval):
        return 0

    if not ensure_opencode_service():
        log.error("OpenCode 服务不可用，本次放弃（不推送半成品）")
        return 1

    # 非交易日**不跳过**，只在报告里标注今天是交易日还是休市
    trading = is_trading_day(now)
    day_tag = "交易日" if trading else "休市日"

    text = _ask_agent(trading)
    if not text:
        log.error("没拿到 agent 回答，本次不推送")
        return 1

    from tools.dingtalk_push import push_markdown

    header = (f"> **{now.strftime('%m月%d日')} 早报**　{now.strftime('%H:%M')} 生成"
              f"　·　{day_tag}")
    ok = push_markdown(f"{header}\n\n{text}", title="中证1000 早报")
    log.info("文字推送 %s", "成功" if ok else "失败")
    if ok:
        _mark_sent()

    if not a.no_image:
        try:
            from tools.dingtalk_push import push_image
            from tools.market_chart import make_index_chart, summary
            path, levels = make_index_chart("中证1000")
            push_image(path, title="中证1000 点位图")
            log.info("点位图已推送：%s", summary(levels).replace("\n", " | "))
        except Exception as exc:  # noqa: BLE001
            log.warning("点位图推送失败（不影响文字）: %s", exc)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
