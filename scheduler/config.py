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

TICK_SECONDS = 20        # 轮询间隔
OUTPUT_TAIL_LINES = 40   # 落库的日志尾巴行数

# 单实例互斥体名（进程崩了内核自动释放，不会留陈旧锁）
MUTEX_NAME = "Local\\dingtalk_agent_scheduler"
