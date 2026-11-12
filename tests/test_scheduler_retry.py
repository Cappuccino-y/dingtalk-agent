"""调度器「中间次数失败不告警」的单元测试。

背景（2026-11-02）：IC 补判 09:20 抓数据被截断，第 1 次失败立刻推了
「❌ IC 信号补判 执行失败」，3 分钟后自动重试已成功 —— 用户看到的是一张
假告警。现在只有「没机会再试」才告警。

跑法（仓库根目录）：python -m unittest tests.test_scheduler_retry -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scheduler import daemon, runner  # noqa: E402
from scheduler.registry import Job  # noqa: E402


def make_job(tmp: str, max_attempts: int = 2, retry_delay_s: int = 180,
             window_min: int = 40) -> Job:
    return Job(
        id="t_job", name="测试作业",
        argv=[sys.executable, "-c", "import sys; sys.exit(3)"],
        at=(9, 20), window_min=window_min,
        max_attempts=max_attempts, retry_delay_s=retry_delay_s, cwd=tmp,
    )


class TestRetryWindow(unittest.TestCase):
    def test_can_retry_inside_window(self):
        job = make_job(".", max_attempts=2, retry_delay_s=180, window_min=40)
        occ = datetime(2026, 11, 2, 9, 20)
        self.assertTrue(daemon._can_retry(job, occ, 1, occ + timedelta(minutes=1)))

    def test_no_retry_when_attempts_exhausted(self):
        job = make_job(".", max_attempts=2)
        occ = datetime(2026, 11, 2, 9, 20)
        self.assertFalse(daemon._can_retry(job, occ, 2, occ + timedelta(minutes=1)))

    def test_no_retry_when_window_too_short(self):
        """窗口只剩 2 分钟、重试要等 3 分钟 → 这次就是最后一次，必须告警。"""
        job = make_job(".", max_attempts=3, retry_delay_s=180, window_min=40)
        occ = datetime(2026, 11, 2, 9, 20)
        now = occ + timedelta(minutes=38)
        self.assertFalse(daemon._can_retry(job, occ, 1, now))


class TestNotifyOnlyFinal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_log_dir = runner.JOB_LOG_DIR
        runner.JOB_LOG_DIR = self.tmp.name
        self.fail_patch = mock.patch.object(runner.notify, "job_failed")
        self.job_failed = self.fail_patch.start()
        self._db = [mock.patch.object(runner.db, n) for n in
                    ("start_run", "finish_run")]
        for p in self._db:
            p.start()

    def tearDown(self):
        for p in self._db:
            p.stop()
        self.fail_patch.stop()
        runner.JOB_LOG_DIR = self._old_log_dir
        self.tmp.cleanup()

    def test_intermediate_failure_is_silent(self):
        job = make_job(self.tmp.name)
        status = runner.run_job(job, datetime(2026, 11, 2, 9, 20),
                                attempt=1, final=False)
        self.assertEqual(status, "failed")
        self.job_failed.assert_not_called()

    def test_final_failure_alerts(self):
        job = make_job(self.tmp.name)
        runner.run_job(job, datetime(2026, 11, 2, 9, 20), attempt=2, final=True)
        self.job_failed.assert_called_once()
        kwargs = self.job_failed.call_args.kwargs
        self.assertEqual(kwargs.get("attempt"), 2)
        self.assertEqual(kwargs.get("max_attempts"), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
