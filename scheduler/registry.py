"""作业注册表：想加一个定时任务，只在这里加一条。

约定（很重要）：
- 每个作业都是「跑完就退」的短命进程，自己算完就退出，**不要在里面写 while+sleep**；
- 作业必须幂等：同一个 occurrence（计划时刻）重复执行也不能重复推送；
- 时间窗口 window_min 表示「错过之后还能补跑多久」，超出就不再补（避免半夜补一条早报）。

occurrence 就是「这次该在哪一刻跑」，也是去重键：
    2026-10-21T08:00   ← premarket 在 10-21 的计划时刻

**IC 监控（抄底/顶部信号）归本 bot**（2026-10-23 从 MainToy 那条链收回）：
用户明确「这个 icmonitor 完全由你控制」→ 启动、状态、去重、推送都归本仓库：
- 定时：本仓库 `ic_monitor`(08:50) / `ic_recheck`(09:20) / `ic_weekly`(周一 09:05)
- 脚本：`jobs/ic_signal_monitor.py`（状态/历史在 `var/ic/`，已并入 MainToy 那条链的历史）
- 推送：**本机器人 1:1**（tools.dingtalk_push）→ 署名本机器人，不是「py 小助手」群机器人
- `MainToy\autorun\autoworkflow.py` 已不再拉起 IC 脚本 → 不会双推
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from .config import IC_SCRIPT, IC_WEEKLY_SCRIPT, PYTHON, REPO

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
    scheduled: bool = True               # False = 只做手动入口，不参与定时（作业归别人管）
    aliases: tuple[str, ...] = ()        # 聊天快捷名，不含斜杠：/ic → ic_monitor
    cwd: str = field(default=REPO)

    @property
    def occurrence_fmt(self) -> str:
        return "%Y-%m-%dT%H:%M"


# bot 自己的作业。IC 监控/补判/周报 2026-10-23 起收回本 bot（见模块头注释）：
# 定时、状态、去重、推送全部由本仓库负责。
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
    # IC 信号监控：**归本 bot**（2026-10-23 从 MainToy 那条链收回）。
    # 08:50 主判：触发就推信号，没触发就推每日心跳日报。
    # 2026-10-24：**周末也跑**（用户要求）—— 周末 akshare 最新数据 = 周五收盘，
    # 周末这一轮是第一次判到周五收盘数据，推「非交易日回顾」；脚本内部按数据日去重。
    # 去重靠脚本自己的历史文件（var/ic/ic_signal_history.json），
    # 所以 09:20 补判、手动 /ic 都不会把同一条信号推两遍。
    Job(
        id="ic_monitor",
        name="IC 信号监控",
        argv=[PYTHON, IC_SCRIPT, "--once"],
        at=(8, 50),
        window_min=70,           # 8:50~10:00，和脚本原本的早盘窗口一致
        dow=None,                # 每天都跑（周末跑非交易日回顾）
        timeout_s=600,
        max_attempts=3,
        retry_delay_s=180,
        notify_on_fail=True,     # 失败往本机器人 1:1 推告警
        aliases=("ic", "IC监控", "IC信号"),
        cwd=REPO,
    ),
    # 09:20 补判：08:50 那轮若因数据滞后被拒判（数据完整性守门），
    # 这里再判一次，只补「真信号」不发心跳（--triggers-only）。
    # 之前 MainToy 那条链是常驻进程每 10 分钟复查到 10:00；这里用一次补判等价收敛。
    Job(
        id="ic_recheck",
        name="IC 信号补判",
        argv=[PYTHON, IC_SCRIPT, "--once", "--triggers-only"],
        at=(9, 20),
        window_min=40,           # 9:20~10:00
        dow=WEEKDAYS,
        timeout_s=600,
        max_attempts=2,
        retry_delay_s=180,
        notify_on_fail=True,
        cwd=REPO,
    ),
    # IC 周报：周一 09:05 出图并推 1:1（用同一份收盘数据）。
    Job(
        id="ic_weekly",
        name="IC 周报",
        argv=[PYTHON, IC_WEEKLY_SCRIPT],
        at=(9, 5),               # 排在 IC 监控之后，用的是同一份收盘数据
        window_min=100,
        dow=(0,),                # 只在周一
        timeout_s=900,
        max_attempts=2,
        retry_delay_s=300,
        notify_on_fail=True,
        aliases=("周报", "weekly"),
        cwd=REPO,
    ),
    Job(
        id="evolve",
        name="知识与行为改进扫描",
        # 读 journal + 记忆索引 + 问答存档 -> LLM 提提案 -> 推钉钉审批卡。
        # 提案者直连 LLM（无工具），落地由 tools/apply_proposal.py 确定性完成。
        argv=[PYTHON, os.path.join(REPO, "tools", "evolve.py")],
        # 这台机器每天 08:40 才开机，凌晨 3 点永远跑不到（窗口过了就永久跳过）。
        # 改成 08:00 + 12 小时补跑窗口 = "当天机器开着就会跑一次"：
        #   08:40 开机 -> 开机后立刻补跑（审批卡 ~08:43 就到手机），开机晚也一样；
        #   窗口拉到 12 小时是防「机器当天一直没开」——下午 20:00 前开机也能补。
        at=(8, 0),                 # 2026-10-24 从 09:20 提前到 08:00（见改动史）
        window_min=720,           # 08:00~20:00 之间机器开着都会补跑
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
    if not job.scheduled:
        return None          # 只做手动入口的作业，永不参与定时
    for day in (now.date(), now.date() - timedelta(days=1)):
        if job.dow is not None and day.weekday() not in job.dow:
            continue
        occ = datetime.combine(day, time(job.at[0], job.at[1]))
        if occ <= now <= occ + timedelta(minutes=job.window_min):
            return occ
    return None


def next_due(job: Job, now: datetime) -> datetime | None:
    """下一次计划时刻（用于状态展示；不含窗口判断）。"""
    if not job.scheduled:
        return None
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
    if not job.scheduled:
        return "仅手动触发（不参与定时）"
    hh, mm = job.at
    dow = "每天" if job.dow is None else (
        "周一~周五" if tuple(job.dow) == WEEKDAYS else
        "、".join("周" + "一二三四五六日"[d] for d in job.dow))
    return f"{dow} {hh:02d}:{mm:02d}（可补跑 {job.window_min} 分钟）"
