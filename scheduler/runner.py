"""跑一个作业：子进程 + 超时强杀 + 落库 + 失败告警。

为什么用子进程而不是在本进程里 import 作业：
- 作业要装 pandas / akshare，调度器本体必须保持轻量（内存、启动速度）；
- 作业崩了/被强杀不会带走调度器；
- 作业可以随时换实现、换语言、单独手动跑，调度器完全不关心。
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from datetime import datetime

from . import db, notify
from .config import JOB_LOG_DIR, OUTPUT_TAIL_LINES
from .registry import Job

log = logging.getLogger("scheduler.runner")

_CREATE_NO_WINDOW = 0x08000000
_NEW_PROCESS_GROUP = 0x00000200


def job_log_path(job: Job) -> str:
    return os.path.join(JOB_LOG_DIR, f"{job.id}.log")


def _tail(path: str, lines: int = OUTPUT_TAIL_LINES) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-lines:])
    except OSError:
        return ""


def _kill_tree(pid: int) -> None:
    """强杀整棵进程树：作业自己可能又拉了子进程（akshare 抓取、agent 调用）。"""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=_CREATE_NO_WINDOW)
    else:  # pragma: no cover - 本机是 Windows
        try:
            os.killpg(os.getpgid(pid), 9)
        except OSError:
            pass


def run_job(job: Job, occurrence: datetime, attempt: int = 1,
            trigger: str = "scheduled", actor: str | None = None,
            occurrence_key: str | None = None,
            extra_args: list[str] | None = None) -> str:
    """同步执行一次，返回最终状态（success / failed / timeout）。

    trigger/actor 只用于留痕（scheduled / manual / chat + 谁点的）；
    occurrence_key 给手动跑用独立键（manual:...），不占定时那条记录；
    extra_args 原样拼在作业命令后面（例如 --dry-run）。
    """
    occ = occurrence_key or occurrence.strftime(job.occurrence_fmt)
    argv = list(job.argv) + list(extra_args or [])
    os.makedirs(JOB_LOG_DIR, exist_ok=True)
    logpath = job_log_path(job)

    db.start_run(job.id, occ, attempt, trigger=trigger, actor=actor)
    started = time.time()
    log.info("▶ 开始 %s（%s，第 %d 次，%s）occ=%s", job.id, job.name, attempt,
             trigger, occ)

    with open(logpath, "a", encoding="utf-8", buffering=1) as fh:
        fh.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} {job.id} "
                 f"attempt={attempt} trigger={trigger} occ={occ}"
                 f"{f' actor={actor}' if actor else ''} =====\n")
        fh.write("$ " + " ".join(argv) + "\n")
        fh.flush()
        try:
            proc = subprocess.Popen(
                argv, cwd=job.cwd,
                stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                creationflags=_CREATE_NO_WINDOW | _NEW_PROCESS_GROUP,
                close_fds=True)
        except Exception as e:  # noqa: BLE001
            fh.write(f"[runner] 启动失败: {e}\n")
            db.finish_run(job.id, occ, "failed", None, time.time() - started,
                          f"[runner] 启动失败: {e}")
            notify.job_failed(job.id, job.name, occ, "failed", None, str(e))
            log.error("✖ %s 启动失败：%s", job.id, e)
            return "failed"

        exit_code: int | None = None
        status = "success"
        try:
            exit_code = proc.wait(timeout=job.timeout_s)
            status = "success" if exit_code == 0 else "failed"
        except subprocess.TimeoutExpired:
            status = "timeout"
            _kill_tree(proc.pid)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass

    duration = time.time() - started
    tail = _tail(logpath)
    db.finish_run(job.id, occ, status, exit_code, duration, tail)
    log.info("%s %s（%.1fs，exit=%s）", "✔" if status == "success" else "✖",
             job.id, duration, exit_code)

    if status != "success" and job.notify_on_fail:
        notify.job_failed(job.id, job.name, occ, status, exit_code, tail)
    return status


def tail_log(job: Job, lines: int = 40) -> str:
    return _tail(job_log_path(job), lines)


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    from .registry import JOBS
    print(run_job(JOBS[0], datetime.now()))
