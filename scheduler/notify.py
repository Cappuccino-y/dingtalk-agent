"""统一的告警出口。

作业自己的「业务推送」（早报正文、信号卡片）仍由作业自己发——那部分内容它最清楚。
这里只负责**调度层面**的推送：失败、超时、长时间没跑成功。原则：
- 去重：同一个 key 只发一次（落 notifications 表），重启/重试都不会刷屏；
- 失败不影响调度：推送异常只记日志，绝不把验证逻辑带崩；
- 走机器人同一个出口（tools.dingtalk_push），不另开 webhook。
"""
from __future__ import annotations

import logging
import os
import sys

from . import db

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

log = logging.getLogger("scheduler.notify")


def send(text: str, title: str = "调度器", key: str | None = None) -> bool:
    """推一条钉钉消息；key 非空时按 key 去重。返回是否真的发出去了。"""
    if key and db.notice_seen(key):
        log.info("告警已发过，跳过：%s", key)
        return False
    try:
        from tools.dingtalk_push import push_markdown
        ok = bool(push_markdown(text, title=title))
    except Exception as e:  # noqa: BLE001
        log.warning("告警推送失败（不影响调度）：%s", e)
        return False
    if ok and key:
        db.mark_notice(key)
    log.info("告警已推送：%s（%s）", key or "-", "成功" if ok else "失败")
    return ok


def job_failed(job_id: str, job_name: str, occurrence: str, status: str,
               exit_code: int | None, tail: str = "") -> bool:
    """作业失败/超时的统一告警（每个 occurrence 只发一次）。"""
    icon = "⏱" if status == "timeout" else "❌"
    cols = [
        f"**{icon} {job_name}**（{job_id}）",
        f"计划时刻：{occurrence.replace('T', ' ')}",
        f"结果：{status}" + (f"，退出码 {exit_code}" if exit_code is not None else ""),
    ]
    if tail.strip():
        cols += ["", "**日志尾部**"]
        cols += [f"> {ln}" for ln in tail.strip().splitlines()[-12:]]
    cols += ["", "回 `/jobs` 看当前状态。"]
    return send("\n".join(cols), title=f"{job_name} 执行失败",
                key=f"fail:{job_id}:{occurrence}")
