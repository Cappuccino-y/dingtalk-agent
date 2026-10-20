"""运行记录（SQLite）。

只存两件事：
1. runs —— 每个 (作业, 计划时刻) 一条：状态、次数、耗时、退出码、日志尾巴。
   「今天跑没跑、成功没、失败几次」全部从这里查，不再去翻各目录的 log。
2. notifications —— 推送去重键，挡住「同一条告警重复刷屏」。

为什么要落库而不是写 json：并发线程 + 重启补跑都要看到一致状态，
SQLite 自带事务和唯一约束，比手搓文件锁可靠。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from .config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    job         TEXT NOT NULL,
    occurrence  TEXT NOT NULL,
    status      TEXT NOT NULL,      -- running | success | failed | timeout | orphan
    attempt     INTEGER NOT NULL DEFAULT 1,
    started_at  TEXT,
    finished_at TEXT,
    exit_code   INTEGER,
    duration_s  REAL,
    output_tail TEXT,
    trigger     TEXT NOT NULL DEFAULT 'scheduled',  -- scheduled | manual | chat
    actor       TEXT,                               -- 谁点的（手动时记 staff id / 用户名）
    PRIMARY KEY (job, occurrence)
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);

CREATE TABLE IF NOT EXISTS notifications (
    key     TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """老库补列：CREATE TABLE IF NOT EXISTS 不会给已有表加字段。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
    for name, ddl in (("trigger", "TEXT NOT NULL DEFAULT 'scheduled'"),
                      ("actor", "TEXT")):
        if name not in cols:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {ddl}")



def init() -> None:
    with _connect():
        pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------------ runs
def get_run(job: str, occurrence: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE job=? AND occurrence=?",
            (job, occurrence)).fetchone()
    return dict(row) if row else None


def start_run(job: str, occurrence: str, attempt: int,
              trigger: str = "scheduled", actor: str | None = None) -> None:
    """标记为 running（新的一轮尝试）。trigger 区分定时/手动。"""
    with _connect() as conn:
        conn.execute(
            """INSERT INTO runs (job, occurrence, status, attempt, started_at,
                                 trigger, actor)
               VALUES (?, ?, 'running', ?, ?, ?, ?)
               ON CONFLICT(job, occurrence) DO UPDATE SET
                 status='running', attempt=excluded.attempt,
                 started_at=excluded.started_at, finished_at=NULL,
                 exit_code=NULL, duration_s=NULL, output_tail=NULL,
                 trigger=excluded.trigger, actor=excluded.actor""",
            (job, occurrence, attempt, _now(), trigger, actor))


def finish_run(job: str, occurrence: str, status: str,
               exit_code: int | None = None, duration_s: float | None = None,
               output_tail: str | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE runs SET status=?, finished_at=?, exit_code=?,
                               duration_s=?, output_tail=?
               WHERE job=? AND occurrence=?""",
            (status, _now(), exit_code, duration_s, output_tail, job, occurrence))


def runs_since(since_iso: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM runs WHERE started_at >= ?
               ORDER BY started_at DESC""",
            (since_iso,)).fetchall()
    return [dict(r) for r in rows]


def recent(limit: int = 20) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]


def running_rows(job: str | None = None) -> list[dict[str, Any]]:
    """所有 running 记录（跨进程可见，用来做单飞判断）。"""
    sql = ("SELECT * FROM runs WHERE status='running'"
           + (" AND job=?" if job else "")
           + " ORDER BY started_at DESC")
    with _connect() as conn:
        rows = conn.execute(sql, (job,) if job else ()).fetchall()
    return [dict(r) for r in rows]


def latest_run(job: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE job=? ORDER BY started_at DESC LIMIT 1",
            (job,)).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------- notifications
def notice_seen(key: str) -> bool:
    with _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM notifications WHERE key=?", (key,)).fetchone() is not None


def mark_notice(key: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO notifications (key, sent_at) VALUES (?, ?)",
            (key, _now()))


def prune_notices(days: int = 30) -> None:
    """清掉过期去重键，避免表无限长。"""
    cutoff = (datetime.now().replace(microsecond=0)
              - timedelta(days=days)).isoformat()
    with _connect() as conn:
        conn.execute("DELETE FROM notifications WHERE sent_at < ?", (cutoff,))
