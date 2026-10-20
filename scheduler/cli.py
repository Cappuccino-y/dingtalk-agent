"""命令行入口：python -m scheduler <子命令>。

    serve            常驻运行（开机自启脚本拉起的就是这个）
    status           今天各作业状态 + 下次时间（机器人 /jobs 也调它）
    list             作业清单与时间表
    run <作业> [参数] 手动立刻跑一次（同步，能看到结果；参数原样传给作业）
    logs <作业> [-n]  看该作业的日志尾巴
    history [-n]     最近运行记录（含定时/手动、谁点的）
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

from . import db, manual, runner
from .config import LOG_PATH
from .registry import JOBS, next_due, schedule_text

log = logging.getLogger("scheduler.cli")


def _setup_logging() -> None:
    # 控制台/重定向默认是 cp936，日志里的 emoji 会炸编码；先钉成 utf-8
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(LOG_PATH, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)


def _find(job_id: str):
    for job in JOBS:
        if job.id == job_id:
            return job
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scheduler",
                                 description="本地作业调度器")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="常驻运行")
    sub.add_parser("status", help="今天的状态")
    sub.add_parser("list", help="作业清单")
    p_run = sub.add_parser("run", help="手动跑一次")
    p_run.add_argument("job")
    p_run.add_argument("extra", nargs=argparse.REMAINDER,
                       help="原样传给作业的参数，例如 --dry-run")
    p_logs = sub.add_parser("logs", help="看日志尾巴")
    p_logs.add_argument("job")
    p_logs.add_argument("-n", "--lines", type=int, default=40)
    p_hist = sub.add_parser("history", help="最近运行记录")
    p_hist.add_argument("-n", "--lines", type=int, default=20)
    a = ap.parse_args(argv)

    _setup_logging()
    db.init()

    if a.cmd in (None, "serve"):
        from .daemon import serve
        return serve()

    if a.cmd == "status":
        from .daemon import status_text
        print(status_text())
        return 0

    if a.cmd == "list":
        print("作业清单：")
        for job in JOBS:
            nxt = next_due(job, datetime.now())
            print(f"  {job.id:<12} {job.name:<12} {schedule_text(job):<28}"
                  f" 下次 {nxt.strftime('%m-%d %H:%M') if nxt else '-'}")
            alias = "　快捷：" + " ".join("/" + a for a in job.aliases) if job.aliases else ""
            print(f"  {'':<12} 命令：{' '.join(job.argv[1:])}{alias}")
        return 0

    if a.cmd == "run":
        job = manual.find_job(a.job)
        if not job:
            print(f"没有这个作业：{a.job}（可用：{', '.join(j.id for j in JOBS)}）")
            return 2
        extra = [x for x in (a.extra or []) if x != "--"]
        actor = os.environ.get("SCHEDULER_ACTOR") or os.environ.get("USERNAME")
        res = manual.run_manual(job, extra_args=extra, actor=actor)
        if res.get("status") == "busy":
            print(f"⚠ {res['reason']}，本次未启动（等它跑完再试）")
            return 3
        note = f"（参数：{' '.join(extra)}）" if extra else ""
        print(f"结果：{res['status']}{note}　occ={res['occurrence']}")
        return 0 if res.get("ok") else 1

    if a.cmd == "history":
        rows = db.recent(a.lines)
        if not rows:
            print("还没有运行记录")
            return 0
        for r in rows:
            trig = r.get("trigger") or "scheduled"
            actor = f" by {r['actor']}" if r.get("actor") else ""
            print(f"{(r['started_at'] or '')[:19]}  {r['job']:<11} {trig:<10}"
                  f"{r['status']:<8} {r['occurrence']}{actor}")
        return 0

    if a.cmd == "logs":
        job = _find(a.job)
        if not job:
            print(f"没有这个作业：{a.job}")
            return 2
        print(runner.tail_log(job, a.lines))
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
