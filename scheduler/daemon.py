"""调度器主循环：唯一常驻的那一个「时间表」。

语义（每个作业每天一个 occurrence）：
1. 到点且在 window_min 窗口内 → 拉起；
2. 已经 success → 永不重跑（重启、崩溃恢复都一样）；
3. 失败/超时 → 窗口内按 retry_delay_s 重试，最多 max_attempts 次；
4. 进程在跑到一半时调度器被杀 → 下次启动把 stale running 标成 orphan 并允许重试；
5. 超过窗口还没成功的 → 放弃（不再半夜补早报），状态留在库里可查。

并发的作业各跑一个线程（早报 agent 很慢，不能挡住 8:50 的 IC 抓取）。
"""
from __future__ import annotations

import atexit
import ctypes
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta

from . import db, manual, notify, runner
from .config import MUTEX_NAME, PID_PATH, TICK_SECONDS
from .registry import JOBS, Job, next_due, occurrence_for, schedule_text

log = logging.getLogger("scheduler.daemon")
_mutex_handle = None
_inflight: dict[tuple[str, str], threading.Thread] = {}
_inflight_lock = threading.Lock()

_STATUS_ICON = {
    "success": "✔", "failed": "✖", "timeout": "⏱",
    "running": "▶", "orphan": "⚠",
}


# ------------------------------------------------------------------ 单实例
def acquire_single_instance() -> bool:
    global _mutex_handle
    try:
        _mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, False,
                                                            MUTEX_NAME)
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return False
    except Exception as e:  # noqa: BLE001
        log.warning("单实例保护不可用（%s），继续启动", e)
    try:
        os.makedirs(os.path.dirname(PID_PATH), exist_ok=True)
        with open(PID_PATH, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        atexit.register(lambda: os.path.exists(PID_PATH) and os.remove(PID_PATH))
    except OSError as e:
        log.warning("写 PID 文件失败: %s", e)
    return True


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


# ------------------------------------------------------------------ 决策
def decide(job: Job, now: datetime) -> tuple[datetime, int] | None:
    """该不该跑；要跑就返回 (occurrence, 第几次尝试)。"""
    occ = occurrence_for(job, now)
    if occ is None:
        return None
    occ_key = occ.strftime(job.occurrence_fmt)
    with _inflight_lock:
        if (job.id, occ_key) in _inflight:
            return None

    # 同一作业已有实例在跑（手动触发、或另一条 occurrence）→ 本次不插队。
    # 等价 k8s CronJob 的 concurrencyPolicy: Forbid；查库所以跨进程有效。
    if manual.busy_run(job, now, exclude=occ_key):
        return None

    row = db.get_run(job.id, occ_key)
    if row is None:
        return occ, 1
    if row["status"] == "success":
        return None

    if row["status"] == "running":
        started = _parse(row["started_at"])
        if started and (now - started).total_seconds() <= job.timeout_s + 120:
            return None        # 还在跑
        # 调度器上次被杀 / 作业假死：收尾成 orphan，允许重试
        db.finish_run(job.id, occ_key, "orphan", row["exit_code"], None,
                      "调度器重启或作业假死，标记为未收尾")
        row = db.get_run(job.id, occ_key) or row

    attempt = int(row["attempt"] or 0) + 1
    if attempt > job.max_attempts:
        return None
    started = _parse(row["started_at"])
    if started and (now - started).total_seconds() < job.retry_delay_s:
        return None            # 还没到重试时间
    return occ, attempt


def _launch(job: Job, occ: datetime, attempt: int) -> None:
    key = (job.id, occ.strftime(job.occurrence_fmt))

    def _worker():
        try:
            runner.run_job(job, occ, attempt)
        except Exception as e:  # noqa: BLE001
            log.exception("作业 %s 执行异常：%s", job.id, e)

    th = threading.Thread(target=_worker, name=f"job-{job.id}", daemon=True)
    with _inflight_lock:
        _inflight[key] = th
    th.start()


def _reap() -> None:
    """清掉已结束的线程登记。"""
    with _inflight_lock:
        for key, th in list(_inflight.items()):
            if not th.is_alive():
                _inflight.pop(key, None)


# ------------------------------------------------------------------ 展示
def status_lines(now: datetime | None = None) -> list[str]:
    """给 /jobs 和命令行用：今天各作业的状态 + 下次时间。

    手动跑的那次也算「今天跑过」，并用「手动」标出来——否则 `/jobs` 看不见手点。
    """
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    latest: dict[str, dict] = {}
    manual_count: dict[str, int] = {}
    for row in db.runs_since(today + "T00:00:00"):
        latest.setdefault(row["job"], row)
        if (row.get("trigger") or "scheduled") != "scheduled":
            manual_count[row["job"]] = manual_count.get(row["job"], 0) + 1

    lines = [f"**作业状态**　{now.strftime('%m-%d %H:%M')}"]
    ok = fail = 0
    for job in JOBS:
        row = latest.get(job.id)
        if row is None:
            state = "○ 今天还没跑"
        else:
            icon = _STATUS_ICON.get(row["status"], "?")
            hhmm = (row["started_at"] or "")[11:16]
            dur = f"（{row['duration_s']:.0f}s）" if row["duration_s"] else ""
            tag = "·手动" if (row.get("trigger") or "scheduled") != "scheduled" else ""
            state = f"{icon} {hhmm}{tag} {row['status']}{dur}"
            if row["status"] == "success":
                ok += 1
            elif row["status"] in ("failed", "timeout", "orphan"):
                fail += 1
        if manual_count.get(job.id):
            state += f"　今日手动 ×{manual_count[job.id]}"
        nxt = next_due(job, now)
        nxt_txt = nxt.strftime("%m-%d %H:%M") if nxt else "-"
        lines.append(f"- **{job.name}** {job.at[0]:02d}:{job.at[1]:02d}"
                     f"　{state}　下次 {nxt_txt}")

    with _inflight_lock:
        busy = {k[0] for k in _inflight}
    for row in db.running_rows():
        job = next((j for j in JOBS if j.id == row["job"]), None)
        started = _parse(row["started_at"])
        if job and started and (now - started).total_seconds() > job.timeout_s + 120:
            continue        # 假死残留，等 reap_stale 收尾，不当成「运行中」
        busy.add(row["job"])
    tail = f"合计：成功 {ok}，异常 {fail}"
    if busy:
        tail += f"，运行中：{'、'.join(sorted(busy))}"
    lines += ["", tail,
              "手动跑：`/run <作业> [参数]`　或 `python -m scheduler run <作业>`"]
    return lines


def status_text(now: datetime | None = None) -> str:
    return "\n".join(status_lines(now))


# ------------------------------------------------------------------ 主循环
def serve() -> int:
    logging.getLogger().setLevel(logging.INFO)
    if not acquire_single_instance():
        log.error("❌ 已有调度器实例在跑（单实例保护），本次退出")
        return 0

    db.init()
    db.prune_notices()
    log.info("=" * 60)
    log.info("调度器启动 pid=%s", os.getpid())
    for job in JOBS:
        nxt = next_due(job, datetime.now())
        log.info("  %-12s %-16s 下次 %s", job.id, schedule_text(job),
                 nxt.strftime("%m-%d %H:%M") if nxt else "-")
    log.info("=" * 60)

    while True:
        now = datetime.now()
        try:
            _reap()
            for orphan in manual.reap_stale(now):
                log.warning("收尾残留的 running：%s", orphan)
            for job in JOBS:
                hit = decide(job, now)
                if hit:
                    _launch(job, hit[0], hit[1])
        except Exception as e:  # noqa: BLE001
            log.exception("调度循环异常（下轮继续）：%s", e)
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout)
    sys.exit(serve())
