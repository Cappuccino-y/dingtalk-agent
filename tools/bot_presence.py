"""机器人「在不在线」的对外告知（presence）。

**要解决的问题**：重启 / 崩溃期间，钉钉 Stream 这边根本没有连接，用户发来的
消息**不会补发**（钉钉不会缓存到下次连接）。于是聊天窗口一片安静——看起来
不是「在重启」，而是「压根没起来」。

这里用三个时间点把状态说清楚：

    要重启  ──本进程主动推──►  🔄 正在重启，约 10 秒；这期间发的消息可能收不到
    停机中  ──看门狗主动推──►  ⚠️ 机器人掉线，正在自动拉起（只在意外掉线时）
    起来了  ──新进程主动推──►  ✅ 已恢复（中断 N 秒）；期间的消息没收到，麻烦重发

实现：
- ``presence.json`` 存 ``last_alive``（心跳，5 秒刷一次）+ 停机/已通知标记，
  跨进程可读，所以新进程能算出**真实停机时长**。
- 只有「停机够久」或「提前打过招呼」才推「已恢复」，正常热启动不打扰用户。
- 所有推送都是 best-effort：失败只写日志，绝不影响启动。

CLI（供看门狗 / 手工排查用）：
    python tools/bot_presence.py --status     # 看状态文件
    python tools/bot_presence.py --down       # 看门狗：机器人掉线了，推一条告知
    python tools/bot_presence.py --failed     # 看门狗：自动拉起失败，推一条告知
"""
import json
import logging
import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_MAINTOY = os.path.dirname(_HERE)
if _MAINTOY not in sys.path:
    sys.path.insert(0, _MAINTOY)

log = logging.getLogger("bot_presence")

# 停机超过这个秒数才值得告诉用户「刚才掉线了」（心跳间隔 5 秒，有 5 秒误差）
MIN_NOTIFY_SECONDS = 20.0
# 「已恢复」之后，用户 N 秒内再发消息时，回复前先补一句状态
GAP_NOTE_TTL = 900.0
# 看门狗「掉线」通知的静默期（秒）：同一次掉线只喊一次
DOWN_NOTICE_COOLDOWN = 180.0
FAILED_NOTICE_COOLDOWN = 600.0
# last_alive 超过这个秒数才掉线，说明机器人本来就停着（不是刚崩的），别推送
DOWN_NOTICE_MAX_STALE = 3600.0

_HEARTBEAT_STARTED = False
_PENDING_NOTE = {"text": "", "at": 0.0}


# ---------------------------------------------------------------- 状态文件
def state_path() -> str:
    base = (os.getenv("BRIDGE_DATA_DIR")
            or os.path.join(_MAINTOY, "bot", "bridge_data"))
    return os.path.join(base, "presence.json")


def _load() -> dict:
    try:
        with open(state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data: dict) -> bool:
    path = state_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError as e:  # noqa: BLE001
        log.debug("写 presence.json 失败：%s", e)
        return False


def _update(**kw) -> dict:
    data = _load()
    data.update(kw)
    _save(data)
    return data


# ---------------------------------------------------------------- 小工具
def _fmt(seconds: float) -> str:
    sec = int(max(0, round(seconds)))
    if sec < 60:
        return f"{sec} 秒"
    if sec < 3600:
        return f"{sec // 60} 分 {sec % 60} 秒"
    return f"{sec // 3600} 小时 {(sec % 3600) // 60} 分"


def _hhmm(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _push(text: str, title: str) -> bool:
    """主动推一条 markdown；任何异常都吞掉（presence 功能不能拖累主流程）。"""
    try:
        from tools.dingtalk_push import push_markdown
        ok = push_markdown(text, title=title)
        (log.info if ok else log.warning)("presence 推送%s：%s", "成功" if ok else "失败", title)
        return bool(ok)
    except Exception as e:  # noqa: BLE001
        log.warning("presence 推送异常（%s）：%s", title, e)
        return False


def _push_async(text: str, title: str) -> bool:
    """同上，但丢到后台线程推。

    为什么：启动时推「已恢复」如果占住主线程，钉钉 Stream 要晚十几秒才连上
    （取 token + POST 都是网络请求，最坏 25 秒），那这段时间发来的消息又会丢。
    """
    threading.Thread(target=_push, args=(text, title),
                     daemon=True, name="presence-push").start()
    return True


# ---------------------------------------------------------------- 生命周期的三段
def push_restart_notice(reason: str = "", eta: int = 12) -> bool:
    """【要重启】排空开始前主动说一声，别让用户对着空窗口猜。"""
    st = _load()
    # 用「这一次重启请求的时间戳」去重：同一请求只喊一次，新请求不会被旧标记压住
    req_at = _restart_requested_at()
    if st.get("announced_at") and st.get("announced_for") == req_at:
        return False
    now = time.time()
    _update(announced_at=now, announced_reason=reason or "",
            announced_for=req_at)
    text = (f"🔄 **机器人正在重启**\n\n"
            f"- 原因：{reason or '代码/配置更新'}\n"
            f"- 预计不可用约 {eta} 秒\n\n"
            f"这期间发来的消息**我收不到**（钉钉不会补发）。"
            f"等收到「已恢复」再发一次就行。")
    _push(text, "机器人重启中")
    return True


def mark_down(reason: str = "") -> None:
    """【要退出】记下停机起点，供下一个进程算停机时长。"""
    _update(down_since=time.time(), down_reason=reason or "")


def on_start(was_restart: bool | None = None, push: bool = True) -> dict:
    """【起来了】算出上一进程停了多久，必要时推「已恢复」。

    :param was_restart: 启动时是否看到重启标记（True=计划内重启，None/False=掉线）
    :return: 本次启动的状态摘要（也用于日志与 /status）
    """
    st = _load()
    now = time.time()
    prev_alive = float(st.get("last_alive") or 0)
    announced = bool(st.get("announced_at"))
    announced_reason = st.get("announced_reason") or ""
    reason = "重启" if (was_restart or announced) else "掉线"
    gap = (now - prev_alive) if prev_alive else 0.0
    if gap < 0:
        gap = 0.0

    info = {
        "gap_seconds": round(gap, 1),
        "reason": reason,
        "prev_alive": prev_alive,
        "notified": False,
    }

    should_notify = bool(prev_alive) and (gap >= MIN_NOTIFY_SECONDS or announced)
    if should_notify and push:
        why = (f"计划内重启" + (f"（{announced_reason}）" if announced_reason else "")
               if reason == "重启" else "意外掉线")
        text = (f"✅ **机器人已恢复**\n\n"
                f"- 状态：{why}\n"
                f"- 中断：约 {_fmt(gap)}\n"
                f"- 恢复：{_hhmm(now)}\n\n"
                f"停机这段时间发来的消息**我没收到**（钉钉不补发）。"
                f"如果有没回音的，重发一次就行。")
        info["notified"] = _push_async(text, "机器人已恢复")
        _set_pending_note(
            f"⚠️ 刚才机器人{('重启' if reason == '重启' else '掉线')}过，"
            f"中断约 {_fmt(gap)}（{_hhmm(now)} 恢复）。\n\n"
            f"这段时间发的消息我没收到，如果上面那条没回音，重发一次就行。")

    info.update({
        "pid": os.getpid(),
        "started_at": now,
        "started_at_str": _hhmm(now),
        "last_alive": now,
        "down_since": None,
        "down_reason": "",
        "announced_at": None,
        "announced_reason": "",
        "announced_for": None,
        "last_downtime": {
            "seconds": round(gap, 1),
            "reason": reason,
            "ended_at": now,
            "ended_at_str": _hhmm(now),
        },
    })
    # 合并而不是覆盖：看门狗写的通知时间戳（last_down_notice_at 等）要留着，
    # 否则刚起来又掉线时会把同一件事重复喊一遍。
    _save({**st, **info})
    return info


def heartbeat_start(interval: float = 5.0) -> None:
    """后台心跳：让下一个进程知道我们「最后活着」是什么时候。"""
    global _HEARTBEAT_STARTED
    if _HEARTBEAT_STARTED:
        return
    _HEARTBEAT_STARTED = True

    def _loop():
        while True:
            try:
                _update(last_alive=time.time(), pid=os.getpid())
            except Exception:  # noqa: BLE001
                pass
            time.sleep(interval)

    threading.Thread(target=_loop, daemon=True, name="presence-heartbeat").start()


# ---------------------------------------------------------------- 给用户的一句话
def _set_pending_note(text: str) -> None:
    _PENDING_NOTE["text"] = text
    _PENDING_NOTE["at"] = time.time()


def consume_gap_notice() -> str:
    """取一次「刚恢复」的提醒（用完即清，15 分钟内有效）。"""
    if not _PENDING_NOTE["text"]:
        return ""
    if time.time() - _PENDING_NOTE["at"] > GAP_NOTE_TTL:
        _PENDING_NOTE["text"] = ""
        return ""
    text = _PENDING_NOTE["text"]
    _PENDING_NOTE["text"] = ""
    return text


def status_line() -> str:
    """给 /status 用的一行/一段状态。"""
    st = _load()
    started = float(st.get("started_at") or 0)
    alive = float(st.get("last_alive") or 0)
    down = st.get("last_downtime") or {}
    lines = ["🟢 **机器人在线**"]
    if started:
        lines.append(f"- 本次启动：{_hhmm(started)}（已运行 {_fmt(time.time() - started)}）")
    if alive:
        lag = time.time() - alive
        lines.append(f"- 心跳：{_fmt(lag)}前")
    if down:
        lines.append(f"- 最近停机：约 {_fmt(down.get('seconds') or 0)}"
                     f"（{down.get('reason') or '-'}，{down.get('ended_at_str') or '-'} 恢复）")
    lines.append(f"- 进程：pid {st.get('pid') or '-'}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 看门狗入口
def notice_down() -> int:
    """看门狗发现机器人不在时调用：只在**意外掉线**且不久之前还活着时推一条。"""
    st = _load()
    now = time.time()
    if st.get("announced_at") or _restart_pending():
        return 0                       # 计划内重启，本进程已经打过招呼了
    last_alive = float(st.get("last_alive") or 0)
    if not last_alive:
        return 0                       # 从没跑起来过（首次安装），不推
    if now - last_alive > DOWN_NOTICE_MAX_STALE:
        return 0                       # 早就停着了（如手动关掉），别打扰
    if now - float(st.get("last_down_notice_at") or 0) < DOWN_NOTICE_COOLDOWN:
        return 0
    _update(last_down_notice_at=now)
    _push(f"⚠️ **机器人掉线了**\n\n"
          f"- 时间：{_hhmm(now)}\n"
          f"- 最后一次心跳：{_fmt(now - last_alive)}前\n"
          f"- 正在自动拉起（通常 30~60 秒内回来）\n\n"
          f"刚发的消息没回音就是被这次掉线吞掉的，"
          f"等收到「已恢复」后重发一次。", "机器人掉线")
    return 0


def notice_failed() -> int:
    """看门狗自动拉起失败时调用（进程秒退 / PID 文件没出现）。"""
    st = _load()
    now = time.time()
    if now - float(st.get("last_failed_notice_at") or 0) < FAILED_NOTICE_COOLDOWN:
        return 0
    _update(last_failed_notice_at=now)
    _push(f"❌ **机器人自动拉起失败**\n\n"
          f"- 时间：{_hhmm(now)}\n"
          f"- 我没有在自动恢复，需要人工看一下日志：\n"
          f"`bot/logs/v3_stderr.log` / `logs/dingtalk_v3.log`", "机器人起不来")
    return 0


def _restart_pending() -> bool:
    return bool(_restart_requested_at())


def _restart_requested_at() -> float:
    """读重启标记里的 requested_at（没有标记就返回 0）。"""
    try:
        base = (os.getenv("BRIDGE_DATA_DIR")
                or os.path.join(_MAINTOY, "bot", "bridge_data"))
        with open(os.path.join(base, "restart.request"), encoding="utf-8") as fh:
            return float((json.load(fh) or {}).get("requested_at") or 0)
    except (OSError, ValueError, json.JSONDecodeError):
        return 0.0


# ---------------------------------------------------------------- CLI
def _main(argv: list) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--status":
        print(json.dumps(_load(), ensure_ascii=False, indent=2))
        return 0
    if argv[0] == "--down":
        return notice_down()
    if argv[0] == "--failed":
        return notice_failed()
    print(f"未知参数：{argv[0]}")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
