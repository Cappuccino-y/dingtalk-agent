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

from .config import PYTHON, REPO

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
    cwd: str = field(default=REPO)

    @property
    def occurrence_fmt(self) -> str:
        return "%Y-%m-%dT%H:%M"


# 三个业务脚本已经全部搬进本仓库的 jobs/ 目录（2026-10-20），
# 调度语义（时间点/窗口/重试/别名）与老仓库完全一致。
JOBS: list[Job] = [
    Job(
        id="premarket",
        name="盘前早报",
        argv=[PYTHON, os.path.join(REPO, "jobs", "premarket_push.py")],
        at=(8, 0),
        window_min=180,          # 开机时间不固定，11:00 前起来都还值得发
        dow=None,                # 周末/节假日也发（内容自己会换成「回顾+下个交易日」）
        timeout_s=1800,          # agent 可能要跑十几分钟
        max_attempts=1,          # 重跑代价高，且它自己有 min-interval 去重
        aliases=("早报", "premarket"),
        cwd=REPO,
    ),
    Job(
        id="ic_monitor",
        name="IC 信号监控",
        argv=[PYTHON, os.path.join(REPO, "jobs", "ic_signal_monitor.py"),
              "--once"],
        at=(8, 50),
        window_min=70,           # 8:50~10:00，和脚本原本的早盘窗口一致
        timeout_s=600,
        max_attempts=3,
        retry_delay_s=180,
        aliases=("ic", "IC监控", "IC信号"),
        cwd=REPO,
    ),
    Job(
        id="ic_weekly",
        name="IC 周报",
        argv=[PYTHON, os.path.join(REPO, "jobs", "ic_weekly_report.py")],
        at=(9, 5),               # 排在 IC 监控之后，用的是同一份收盘数据
        window_min=100,
        dow=(0,),                # 只在周一
        timeout_s=900,
        max_attempts=2,
        retry_delay_s=300,
        aliases=("周报", "weekly"),
        cwd=REPO,
    ),
    Job(
        id="evolve",
        name="知识与行为改进扫描",
        # 读 journal + 记忆索引 + 问答存档 -> LLM 提提案 -> 推钉钉审批卡。
        # 提案者直连 LLM（无工具），落地由 tools/apply_proposal.py 确定性完成。
        argv=[PYTHON, os.path.join(REPO, "tools", "evolve.py")],
        at=(3, 0),                 # 凌晨 3 点跑，不打扰人
        window_min=60,            # 3:00~4:00 之间起来都还能补跑
        dow=None,                 # 每天都跑（周末也有新交互）
        timeout_s=900,
        max_attempts=1,           # LLM 调用贵，别重复跑
        retry_delay_s=600,
        notify_on_fail=True,
        aliases=("改进", "复盘", "evolve"),
        cwd=REPO,
    ),
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
