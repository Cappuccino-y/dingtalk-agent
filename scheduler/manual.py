"""手动触发：定时作业的第二个入口。

设计要点（对照 Airflow / k8s CronJob / GitHub Actions 的通行做法）：

1. **作业定义唯一**。手动跑不另写一套"手动脚本"，仍然调 `runner.run_job`，
   定时和手动只差「谁触发、记在哪」。
2. **运行记录分开**。手动跑用独立 occurrence 键 `manual:2026-10-20T12:30:05`，
   不会覆盖/污染定时那一条（Airflow 里叫 run_type 不同，K8s 里叫 Job 是手动建的）。
3. **执行层单飞**。同一作业已有 running（不论谁触发）就拒绝，等价于
   `concurrencyPolicy: Forbid`；查库判断，所以跨进程有效（机器人 vs 调度器）。
4. **业务层去重不绕过**。IC 当天推过就不再推，是作业自己的幂等；要重发得显式
   传 `--force`，由 `extra_args` 原样传给作业。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime

from . import db, runner
from .registry import JOBS, Job

log = logging.getLogger("scheduler.manual")

TRIGGER_MANUAL = "manual"
TRIGGER_CHAT = "chat"

_lock = threading.Lock()   # 同一进程内「查忙 + 启动」原子化（机器人并发消息）


def find_job(token: str) -> Job | None:
    """按作业 id / 中文名 / 聊天别名找作业（忽略大小写）。"""
    t = (token or "").strip().lower()
    if not t:
        return None
    for job in JOBS:
        if job.id.lower() == t:
            return job
    for job in JOBS:
        names = (job.name, *job.aliases)
        if any(n.lower() == t for n in names):
            return job
    return None


def manual_key(job: Job, now: datetime | None = None) -> str:
    """手动跑的 occurrence 键；同一秒连点两次时加序号。"""
    now = now or datetime.now()
    base = "manual:" + now.strftime("%Y-%m-%dT%H:%M:%S")
    key, n = base, 1
    while db.get_run(job.id, key) is not None:
        n += 1
        key = f"{base}#{n}"
    return key


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def busy_run(job: Job, now: datetime | None = None,
             exclude: str | None = None) -> dict | None:
    """该作业有没有正在跑的实例；假死超时的顺手收尾成 orphan。"""
    now = now or datetime.now()
    for row in db.running_rows(job.id):
        if exclude and row["occurrence"] == exclude:
            continue
        started = _parse(row["started_at"])
        if started and (now - started).total_seconds() <= job.timeout_s + 120:
            return row
        db.finish_run(job.id, row["occurrence"], "orphan", row["exit_code"],
                      None, "超过超时上限仍未收尾，标记为未完成")
    return None


def reap_stale(now: datetime | None = None) -> list[str]:
    """把所有「超时上限还挂着 running」的记录收尾（调度器每轮调一次）。

    没有这一步，被强杀/断电留下的 running 会永远显示「运行中」，
    也会让单飞闸门一直拒绝新的手动触发。
    """
    now = now or datetime.now()
    reaped: list[str] = []
    for job in JOBS:
        for row in db.running_rows(job.id):
            started = _parse(row["started_at"])
            if started and (now - started).total_seconds() <= job.timeout_s + 120:
                continue
            db.finish_run(job.id, row["occurrence"], "orphan", row["exit_code"],
                          None, "超过超时上限仍未收尾，标记为未完成")
            reaped.append(f"{job.id}:{row['occurrence']}")
    return reaped


def run_manual(job: Job, extra_args: list[str] | None = None,
               actor: str | None = None, trigger: str = TRIGGER_MANUAL,
               allow_overlap: bool = False) -> dict:
    """同步跑一次，返回结果摘要（给 CLI 用）。"""
    if not job.manual_ok:
        return {"ok": False, "status": "refused",
                "reason": f"{job.id} 已设置为不可手动触发"}

    now = datetime.now()
    with _lock:                      # 查忙 + 占位，原子化
        busy = None if allow_overlap else busy_run(job, now)
        if busy:
            return {"ok": False, "status": "busy", "busy": True,
                    "running_since": busy["started_at"],
                    "occurrence": busy["occurrence"],
                    "reason": f"{job.id} 正在运行（{busy['started_at']} 开始）"}
        occ = manual_key(job, now)
        db.start_run(job.id, occ, 1, trigger=trigger, actor=actor)

    status = runner.run_job(job, now, attempt=1, trigger=trigger, actor=actor,
                            occurrence_key=occ, extra_args=extra_args)
    return {"ok": status == "success", "status": status, "busy": False,
            "occurrence": occ, "job": job.id}


def start_manual(job: Job, extra_args: list[str] | None = None,
                 actor: str | None = None, trigger: str = TRIGGER_CHAT,
                 on_done=None, allow_overlap: bool = False) -> dict:
    """异步跑一次，立刻返回「已启动」或 busy。

    机器人进程用它：不能阻塞钉钉回调，跑完再通过 on_done(result) 回推。
    """
    if not job.manual_ok:
        return {"ok": False, "status": "refused",
                "reason": f"{job.id} 已设置为不可手动触发"}

    now = datetime.now()
    with _lock:                      # 查忙 + 占位，原子化
        busy = None if allow_overlap else busy_run(job, now)
        if busy:
            return {"ok": False, "status": "busy", "busy": True,
                    "running_since": busy["started_at"],
                    "occurrence": busy["occurrence"],
                    "reason": f"{job.id} 正在运行（{busy['started_at']} 开始）"}
        occ = manual_key(job, now)
        db.start_run(job.id, occ, 1, trigger=trigger, actor=actor)

    def _worker():
        result: dict
        try:
            status = runner.run_job(job, now, attempt=1, trigger=trigger,
                                    actor=actor, occurrence_key=occ,
                                    extra_args=extra_args)
            result = {"ok": status == "success", "status": status,
                      "busy": False, "occurrence": occ, "job": job.id}
        except Exception as e:  # noqa: BLE001
            log.exception("手动作业 %s 异常：%s", job.id, e)
            result = {"ok": False, "status": "failed", "busy": False,
                      "occurrence": occ, "job": job.id, "reason": str(e)}
        if on_done:
            try:
                on_done(result)
            except Exception as e:  # noqa: BLE001
                log.warning("手动作业 %s 结果回调失败：%s", job.id, e)

    threading.Thread(target=_worker, name=f"manual-{job.id}",
                     daemon=True).start()
    return {"ok": True, "status": "started", "busy": False,
            "occurrence": occ, "job": job.id}


def jobs_text() -> str:
    """手动可跑的作业清单（聊天里 /run 无参数时用）。"""
    lines = []
    for job in JOBS:
        alias = ("、".join("/" + a for a in job.aliases[:1])
                 if job.aliases else "")
        lines.append(f"- `{job.id}` {job.name}　{alias}")
    return "\n".join(lines)
