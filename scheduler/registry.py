"""作业注册表：想加一个定时任务，只在这里加一条。

约定（很重要）：
- 每个作业都是「跑完就退」的短命进程，自己算完就退出，**不要在里面写 while+sleep**；
- 作业必须幂等：同一个 occurrence（计划时刻）重复执行也不能重复推送；
- 时间窗口 window_min 表示「错过之后还能补跑多久」，超出就不再补（避免半夜补一条早报）。

occurrence 就是「这次该在哪一刻跑」，也是去重键：
    2026-10-21T08:00   ← premarket 在 10-21 的计划时刻

**IC 监控（抄底/顶部信号）已交回 MainToy 那条链**（2026-10-22）：
`MainToy\autorun\autoworkflow.py` 拉起 `MainToy\finanCalc\ic_signal_monitor.py`（常驻模式），
推送走 MainToy\tools\notifycation.py（py 小助手群 webhook）—— 不经本机器人的 1:1 通道。
所以本仓库的 ic_monitor / ic_weekly **不参与定时**（scheduled=False），只留手动入口：
它们指向**同一份脚本、同一份状态/历史文件**（IC_HOME），所以手动跑和那条链共用去重，
同一条信号只会被推一次，不会双推。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from .config import IC_HOME, IC_SCRIPT, IC_WEEKLY_SCRIPT, PYTHON, REPO

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


# bot 自己的作业。IC 监控/周报 2026-10-22 起交回 MainToy 那条链（见模块头注释）：
# 本仓库只保留**手动入口**（scheduled=False），定时由 autoworkflow 那条链负责。
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
    # IC 信号监控：**只做手动入口**，定时由 MainToy\\autorun\\autoworkflow.py 负责。
    # 两边跑的是同一份脚本、同一份状态/历史文件（IC_HOME），所以：
    #   - 谁先跑谁推，状态文件保证当天只推一次（不会双推）；
    #   - /ic 手动跑 = 立刻补一次判定，和那条链共用去重。
    Job(
        id="ic_monitor",
        name="IC 信号监控（手动）",
        # --triggers-only：手动跑只补「真信号」，不发每日心跳——
        # 心跳由 autoworkflow 那条链的常驻进程发，状态文件不共享，
        # 两边都发就会在群里刷两条一样的日报。
        argv=[PYTHON, IC_SCRIPT, "--once", "--triggers-only"],
        at=(8, 50),
        window_min=70,           # 8:50~10:00，和脚本原本的早盘窗口一致
        timeout_s=600,
        max_attempts=3,
        retry_delay_s=180,
        scheduled=False,         # 不参与定时（定时归 autoworkflow 那条链）
        notify_on_fail=False,    # IC 的事归那条链，失败不往本机器人 1:1 推卡
        aliases=("ic", "IC监控", "IC信号"),
        cwd=IC_HOME,
    ),
    # IC 周报：同上，只留手动入口（周一 09:05 那次由 autoworkflow 那条链负责）。
    Job(
        id="ic_weekly",
        name="IC 周报（手动）",
        argv=[PYTHON, IC_WEEKLY_SCRIPT],
        at=(9, 5),               # 排在 IC 监控之后，用的是同一份收盘数据
        window_min=100,
        dow=(0,),                # 只在周一
        timeout_s=900,
        max_attempts=2,
        retry_delay_s=300,
        scheduled=False,         # 不参与定时
        notify_on_fail=False,
        aliases=("周报", "weekly"),
        cwd=IC_HOME,
    ),
    Job(
        id="evolve",
        name="知识与行为改进扫描",
        # 读 journal + 记忆索引 + 问答存档 -> LLM 提提案 -> 推钉钉审批卡。
        # 提案者直连 LLM（无工具），落地由 tools/apply_proposal.py 确定性完成。
        argv=[PYTHON, os.path.join(REPO, "tools", "evolve.py")],
        # 这台机器每天 08:40 才开机，凌晨 3 点永远跑不到（窗口过了就永久跳过）。
        # 改成 09:20 + 12 小时补跑窗口 = "当天机器开着就会跑一次"：
        #   08:40 开机 -> 09:20 准点跑；开机晚了（12:00）-> 开机后立刻补跑。
        at=(9, 20),                # 上午跟早报/IC 监控之后跑
        window_min=720,           # 09:20~21:20 之间机器开着都会补跑
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
