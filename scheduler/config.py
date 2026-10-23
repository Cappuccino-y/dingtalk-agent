"""调度器的路径/常量。测试用 SCHEDULER_HOME 指向临时目录即可隔离。"""
from __future__ import annotations

import os

# 本仓库根目录：D:\PythonProject\dingtalk-agent
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 运行期状态放用户目录，不进仓库（仓库里已有太多运行产物了）
HOME = os.environ.get("SCHEDULER_HOME") or os.path.join(
    os.path.expanduser("~"), ".dingtalk-bot")
DB_PATH = os.path.join(HOME, "scheduler.db")
PID_PATH = os.path.join(HOME, "scheduler.pid")
LOG_PATH = os.path.join(HOME, "logs", "scheduler.log")
JOB_LOG_DIR = os.path.join(HOME, "logs", "jobs")

# 机器人用的解释器（和开机自启、计划任务保持同一个环境）
PYTHON = os.environ.get("SCHEDULER_PYTHON") or \
    r"D:\Scoop\persist\anaconda3\App\envs\MainToy\python.exe"

# ---- IC 监控（抄底/顶部信号）：整条链归本 bot ----
# 2026-10-23 起：IC 监控**收回本机器人**（用户明确：「这个 icmonitor 完全由你控制」）。
#   - 定时：本仓库调度器 ic_monitor(08:50) / ic_recheck(09:20) / ic_weekly(周一 09:05)
#   - 脚本：本仓库 jobs/ic_signal_monitor.py（最新版，状态/历史在 var/ic/）
#   - 推送：本机器人 1:1 通道（tools.dingtalk_push）→ 署名本机器人，不是「py 小助手」群机器人
#   - MainToy\autorun\autoworkflow.py 已不再拉起 IC 脚本 → 不会双推
# 想换位置/脚本改环境变量 IC_HOME / IC_SCRIPT 即可。
IC_HOME = os.environ.get("IC_HOME") or REPO
IC_SCRIPT = os.environ.get("IC_SCRIPT") or os.path.join(REPO, "jobs", "ic_signal_monitor.py")
IC_WEEKLY_SCRIPT = os.environ.get("IC_WEEKLY_SCRIPT") or os.path.join(REPO, "jobs", "ic_weekly_report.py")

TICK_SECONDS = 20        # 轮询间隔
OUTPUT_TAIL_LINES = 40   # 落库的日志尾巴行数

# 单实例互斥体名（进程崩了内核自动释放，不会留陈旧锁）
MUTEX_NAME = "Local\\dingtalk_agent_scheduler"
