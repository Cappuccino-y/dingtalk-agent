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

# ---- IC 监控的"主人"在 MainToy 那条链 ----
# 2026-10-22 起：IC 信号的**定时启动**由 MainToy\autorun\autoworkflow.py 负责
# （拉起 MainToy\finanCalc\ic_signal_monitor.py 常驻模式：8:50 起每 10 分钟复查
#  到 10:00；推送走 MainToy\tools\notifycation.py 的群 webhook = py 小助手）。
# 本仓库只保留**手动入口**（聊天 /ic、/周报），并且指向**同一个脚本**：
# 这样手动跑和定时跑共用同一份状态/历史/去重，不会把同一条信号推两遍。
# 想换位置改环境变量 IC_HOME 即可。
IC_HOME = os.environ.get("IC_HOME") or r"D:\PythonProject\MainToy\finanCalc"
IC_SCRIPT = os.path.join(IC_HOME, "ic_signal_monitor.py")
IC_WEEKLY_SCRIPT = os.path.join(IC_HOME, "ic_weekly_report.py")

TICK_SECONDS = 20        # 轮询间隔
OUTPUT_TAIL_LINES = 40   # 落库的日志尾巴行数

# 单实例互斥体名（进程崩了内核自动释放，不会留陈旧锁）
MUTEX_NAME = "Local\\dingtalk_agent_scheduler"
