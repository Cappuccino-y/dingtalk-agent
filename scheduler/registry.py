"""作业注册表：想加一个定时任务，只在这里加一条。

约定（很重要）：
- 每个作业都是「跑完就退」的短命进程，自己算完就退出，**不要在里面写 while+sleep**；
- 作业必须幂等：同一个 occurrence（计划时刻）重复执行也不能重复推送；
- 时间窗口 window_min 表示「错过之后还能补跑多久」，超出就不再补（避免半夜补一条早报）。

occurrence 就是「这次该在哪一刻跑」，也是去重键：
    2026-10-21T08:50   ← ic_monitor 在 10-21 的计划时刻
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from .config import MAINTOY, PYTHON

WEEKDAYS = (0, 1, 2, 3, 4)   # 周一~周五


@dataclass(frozen=True)
class Job:
    id: str
    name: str
    argv: list[str]
    at: tuple[int, int]                  # 每天几点几分
    window_min: int                      # 错过还能补跑多久（分钟）
    dow: tuple[int, ...] | None = WEEKDAYS  # 允许的星期（None=每天）
    timeout_s: int = 900                 # 超时强杀（含子进程树）
    max_attempts: int = 2                # 窗口内最多试几次
    retry_delay_s: int = 300             # 失败后隔多久再试
    notify_on_fail: bool = True          # 失败是否推钉钉
    manual_ok: bool = True               # 允许手动/聊天触发（定时之外的第二个入口）
    aliases: tuple[str, ...] = ()        # 聊天快捷名，不含斜杠：/ic → ic_monitor
    cwd: str = field(default=MAINTOY)

    @property
    def occurrence_fmt(self) -> str:
        return "%Y-%m-%dT%H:%M"


JOBS: list[Job] = [
    # 本仓库只带交互机器人，交易作业（盘前早报 / IC 监控 / IC 周报）
    # 仍留在旧项目 D:\PythonProject\MainToy\scheduler\registry.py。
    # 要在这里加作业：把脚本搬进本仓库，然后照上面的 Job(...) 写法加一条。
]


# --------------------------------------------------------------- 时间计算
def occurrence_for(job: Job, now: datetime) -> datetime | None:
    """今天（含跨零点窗口的前一天）仍在窗口内的计划时刻；没有就 None。"""
    for day in (now.date(), now.date() - timedelta(days=1)):
        if job.dow is not None and day.weekday() not in job.dow:
            continue
        occ = datetime.combine(day, time(job.at[0], job.at[1]))
        if occ <= now <= occ + timedelta(minutes=job.window_min):
            return occ
    return None


def next_due(job: Job, now: datetime) -> datetime | None:
    """下一次计划时刻（用于状态展示；不含窗口判断）。"""
    for offset in range(0, 8):
        day = now.date() + timedelta(days=offset)
        if job.dow is not None and day.weekday() not in job.dow:
            continue
        occ = datetime.combine(day, time(job.at[0], job.at[1]))
        if occ > now:
            return occ
    return None


def schedule_text(job: Job) -> str:
    """人类可读的时间表。"""
    hh, mm = job.at
    dow = "每天" if job.dow is None else (
        "周一~周五" if tuple(job.dow) == WEEKDAYS else
        "、".join("周" + "一二三四五六日"[d] for d in job.dow))
    return f"{dow} {hh:02d}:{mm:02d}（可补跑 {job.window_min} 分钟）"
