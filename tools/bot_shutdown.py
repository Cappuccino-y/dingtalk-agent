"""协同关机 —— 与「协同重启」对称的离线机制。

参考的是这一类机器人/守护进程的通用做法（跨语言跨产品的共识）：

- **优雅退出**：先停止收新活（draining），再发一条告别通知，最后才断电退进程。
  IRC 叫它 quit message（`Client Quit` = 没写告别的退出，`Quit: <原因>` = 计划内退出）；
  K8s 叫它 terminationGracePeriod（先摘流量 → SIGTERM → 有界等待 → 再 SIGKILL）。
- **告别必须先送达**：通知是在断开之前推出去的。顺序反了，用户只看到"突然没了"。
- **有界等待**：排空在飞请求有上限，超时就强制收尾，不无限期挂着。
- **独立兜底定时器**：主进程崩了也得有人把机器关掉，不能只靠进程自己。
- **计划内 vs 意外**：离线通知必须说清是「计划内停机」还是「崩了」，否则用户每次都在猜。

本模块只做**确定性**的部分（不经 LLM）：

1. 写关机标记 `bot/bridge_data/shutdown.request`（跨进程可见，看门狗也能读）；
2. 调 Windows `shutdown /s /t N` 起倒计时（**兜底**：bot 崩了也照关）；
3. 支持撤销（删标记 + `shutdown /a`）；
4. 记 journal 审计（谁在什么时候排的、排给什么理由）。

真正的「排空 → 告别 → 断电」由 bot 进程里的 shutdown watcher 执行
（`bot/listen.py::start_shutdown_watcher`），跟协同重启一个路子：
**绝不**在本进程里直接杀自己（那会把正在渲染的卡片变成僵尸卡）。

用法：
    python tools/bot_shutdown.py --schedule 90 --reason "用户指令"
    python tools/bot_shutdown.py --abort
    python tools/bot_shutdown.py --status
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

# 默认倒计时：给「当前回合跑完 + 卡片收尾 + 通知送达」留时间，
# 同时也是 bot 崩掉时的兜底关机时间。
DEFAULT_DELAY = 90
# 排空在飞回合的上限（秒）。超过就强制收尾，别无限期挂着。
DEFAULT_GRACE = 180
# 标记超过这个秒数就算陈旧（机器都重启过了，这个请求早就不成立了）
STALE_AFTER = 3600


# ---------------------------------------------------------------- 标记文件
def _data_dir() -> str:
    return (os.getenv("BRIDGE_DATA_DIR")
            or os.path.join(REPO, "bot", "bridge_data"))


def request_path() -> str:
    return os.path.join(_data_dir(), "shutdown.request")


def shutdown_request() -> dict | None:
    """读关机标记。没有就返回 None。"""
    path = request_path()
    if not os.path.exists(path):
        return None
    try:
        # utf-8-sig：带 BOM 也能读（PowerShell Set-Content -Encoding UTF8 会写 BOM）
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def shutdown_pending() -> bool:
    return shutdown_request() is not None


def clear_shutdown() -> bool:
    """删掉关机标记（只删标记，不动 Windows 的倒计时）。"""
    try:
        path = request_path()
        if os.path.exists(path):
            os.remove(path)
        return True
    except OSError:
        return False


def is_stale(req: dict | None = None) -> bool:
    """这个关机标记还成立吗？

    **陈旧标记必须当不存在**，否则会锁死开机自启：
    标记是磁盘上的文件，机器重启之后它还在。如果看门狗/机器人把它当成
    「还有一次关机待执行」，就永远不会把 bot 拉起来 —— 表现为"开机后
    机器人一直不在线"。

    判定（满足任一即算陈旧）：
    1. 标记写于**本次开机之前**（requested_at < 进程启动时间）；
    2. 标记太老（超过 STALE_AFTER）。
    """
    req = req if req is not None else shutdown_request()
    if not req:
        return True
    ts = float(req.get("requested_at") or 0)
    if not ts:
        return True
    if (time.time() - ts) > STALE_AFTER:
        return True
    # 本进程是开机后才起来的 —— 标记比它还老，说明是上一次开机留下的
    return bool(_PROCESS_START and ts < _PROCESS_START - 5)


# 本进程启动时刻（import 时取一次）——用来判断标记是不是上次开机留下的
_PROCESS_START = time.time()


def clear_if_stale() -> bool:
    """开机时清掉陈旧标记（返回是否清掉了）。"""
    if shutdown_pending() and is_stale():
        clear_shutdown()
        return True
    return False


# ---------------------------------------------------------------- Windows 关机
def _run_shutdown(args: list[str]) -> tuple[int, str]:
    """调 shutdown.exe。返回 (rc, 输出)。"""
    if os.name != "nt":
        return 1, "只支持 Windows"
    try:
        r = subprocess.run(["shutdown", *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=20)
        return r.returncode, (r.stderr or r.stdout or "").strip()
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


def arm_timer(delay: int, mode: str = "force", reason: str = "") -> tuple[bool, str]:
    """起 Windows 关机倒计时（**兜底**：bot 崩了也照关）。

    mode:
      force —— `/t N`（>0 时 /f 本来就隐含）：定时强关，未保存内容会丢。
      grace —— `/t 0` 不带 /f：让应用自己收尾，但可能弹窗等人确认。
    """
    if mode == "grace":
        args = ["/s", "/t", "0", "/c", f"dingtalk-agent 关机（{reason or '用户指令'}）"]
    else:
        args = ["/s", "/t", str(max(0, int(delay))), "/f", "/c",
                f"dingtalk-agent 关机（{reason or '用户指令'}）"]
    rc, msg = _run_shutdown(args)
    return rc == 0, (msg or f"rc={rc}")


def abort_timer() -> tuple[bool, str]:
    """取消 Windows 侧的关机倒计时（`shutdown /a`）。"""
    rc, msg = _run_shutdown(["/a"])
    if rc == 0:
        return True, "已取消系统关机倒计时。"
    # 1116 = 没有待处理的关机（本來就没排）
    return False, (msg or f"rc={rc}")


def execute_now() -> tuple[bool, str]:
    """立刻断电（watcher 排空完回合后调用）。"""
    rc, msg = _run_shutdown(["/s", "/t", "0", "/f", "/c", "dingtalk-agent 关机"])
    return rc == 0, (msg or f"rc={rc}")


# ---------------------------------------------------------------- 排入 / 撤销
def _journal(kind: str, summary: str, **extra) -> None:
    try:
        from tools import memory_store as ms
        ms.append_journal(kind, summary, **extra)
    except Exception:  # noqa: BLE001
        pass


def request_shutdown(delay: int = DEFAULT_DELAY, reason: str = "",
                     by: str = "", mode: str = "force",
                     dry_run: bool = False) -> dict:
    """排入关机：写标记 + 起 Windows 倒计时。

    幂等：重复排入**覆盖**同一个标记，不叠加（也不重复起计时器）。
    """
    delay = max(5, int(delay or DEFAULT_DELAY))
    prev = shutdown_request()
    now = time.time()
    req = {
        "requested_at": now,
        "requested_at_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "delay": delay,
        "mode": mode,
        "reason": reason or "",
        "by": by or "",
        "supersedes": (prev or {}).get("requested_at", 0) or 0,
    }
    if dry_run:
        req["dry_run"] = True
        req["windows"] = "（dry-run，没真的调 shutdown）"
        return req

    os.makedirs(_data_dir(), exist_ok=True)
    with open(request_path(), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(req, fh, ensure_ascii=False, indent=2)

    ok, msg = arm_timer(delay, mode=mode, reason=reason)
    req["windows_rc"] = 0 if ok else 1
    req["windows_msg"] = msg
    _journal("shutdown_requested",
             f"排入关机（{delay}s 后，mode={mode}）：{reason or '用户指令'}",
             by=by or "-", delay=delay, mode=mode, windows_ok=ok)
    return req


def abort_shutdown(by: str = "") -> tuple[bool, str]:
    """撤销关机：删标记 + `shutdown /a`。"""
    had = shutdown_pending()
    clear_shutdown()
    ok, msg = abort_timer()
    if had:
        _journal("shutdown_aborted", "撤销关机请求", by=by or "-", windows_ok=ok)
    if ok:
        return True, "已撤销关机。"
    # Windows 侧没有待关机的关机（说明还没起计时器或已经关过了）——
    # 标记已经删掉，效果上依然是"没关"。
    return had, ("已删关机标记；系统侧没有待处理的关机。" if had
                 else "没有待处理的关机。")


def status_text() -> str:
    """给 /status 用的一段状态。"""
    req = shutdown_request()
    if not req:
        return "没有待处理的关机。"
    left = float(req.get("delay") or 0) - (time.time() - float(req.get("requested_at") or 0))
    lines = ["🔌 **待关机**"]
    lines.append(f"- 排入时间：{req.get('requested_at_str') or '-'}")
    lines.append(f"- 倒计时：约 {int(max(0, left))} 秒（兜底）")
    lines.append(f"- 模式：{req.get('mode') or 'force'}")
    if req.get("reason"):
        lines.append(f"- 理由：{req['reason']}")
    lines.append("撤销：`/取消关机`（或电脑上 `shutdown /a`）")
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser(description="协同关机：排入 / 撤销 / 查看状态")
    ap.add_argument("--schedule", nargs="?", const=DEFAULT_DELAY, type=int,
                    metavar="秒", help="排入关机，默认 %d 秒后兜底断电" % DEFAULT_DELAY)
    ap.add_argument("--abort", action="store_true", help="撤销待处理的关机")
    ap.add_argument("--status", action="store_true", help="查看待关机状态")
    ap.add_argument("--reason", default="", help="关机理由（写进标记和通知）")
    ap.add_argument("--by", default="", help="发起人（审计用）")
    ap.add_argument("--grace", action="store_true",
                    help="温和模式：让应用自己收尾（可能弹窗等人，不适合远程）")
    ap.add_argument("--dry-run", action="store_true", help="只写标记不起计时器")
    args = ap.parse_args()

    if args.status:
        print(status_text())
        return 0

    if args.abort:
        ok, msg = abort_shutdown(by=args.by)
        print(msg)
        return 0 if ok else 1

    if args.schedule is None:
        print(status_text())
        return 0

    req = request_shutdown(delay=args.schedule, reason=args.reason, by=args.by,
                            mode="grace" if args.grace else "force",
                            dry_run=args.dry_run)
    if args.dry_run:
        print("dry-run：**没有**写标记，**没有**调 shutdown，什么都没发生。")
        print("本来要做的：")
        print(json.dumps(req, ensure_ascii=False, indent=2))
        return 0
    print("已排入关机：")
    print(json.dumps(req, ensure_ascii=False, indent=2))
    print("bot 进程会在当前回合跑完后推一条关机通知并立刻断电。")
    print("撤销：python tools/bot_shutdown.py --abort")
    return 0


if __name__ == "__main__":
    sys.exit(main())
