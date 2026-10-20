"""本地作业调度器（只管机器人自己那几件事）。

设计取舍见 docs/scheduler.md。一句话：
    长驻进程只服务「外部事件流」（钉钉 Stream、MCP），不服务「时间表」。
    按时间跑的东西一律是「跑完就退」的短命进程，由这里统一触发、记录、重试、告警。

入口：
    python -m scheduler serve            # 常驻（由开机自启脚本拉起）
    python -m scheduler status           # 今天跑了什么 / 下次什么时候
    python -m scheduler list             # 所有作业与时间表
    python -m scheduler run ic_monitor [--dry-run]   # 手动跑一次（参数原样传给作业）
    python -m scheduler history          # 最近运行记录（定时/手动、谁点的）

手动触发是「第二个入口」而不是第二套实现：仍然走 runner.run_job，
只是记录里 trigger=manual/chat、occurrence 用独立键，并受单飞（Forbid）保护。
"""
from __future__ import annotations

__all__ = ["JOBS", "load_jobs"]

from .registry import JOBS  # noqa: E402


def load_jobs():
    """返回作业注册表（给外部按名字查用）。"""
    return {j.id: j for j in JOBS}
