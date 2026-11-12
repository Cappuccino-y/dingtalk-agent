"""公众号视频日报：每天检查「远道擒龙」当天是否发新文章。

流程：
    17:00 触发 -> 调 wechat-monitor 的 digest（album 列表 -> 提取视频 ->
    下载 -> ASR -> MiniMax 提炼）-> 推钉钉 1:1

设计要点：
- 真正的重活（faster-whisper / CUDA）在 wechat-monitor 自己的 venv 里跑，
  subprocess 隔离，本仓库环境不装 torch；
- 幂等：var/wechat/state.json 记 last_msgid + last_date，同一天同一篇不重推；
- 当天没发新文章 = 正常退出 0，不推空消息。

用法：
    python jobs/wechat_video_digest.py
    python jobs/wechat_video_digest.py --force      # 忽略去重，强制重推
    python jobs/wechat_video_digest.py --dry-run    # 只跑处理，不推送
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for _p in (REPO, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

VAR_DIR = REPO / "var" / "wechat"
VAR_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = VAR_DIR / "state.json"
LOG_PATH = VAR_DIR / "wechat_video.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("wechat_video")

WECHAT_ROOT = Path(os.environ.get("WECHAT_MONITOR_HOME", r"D:\PythonProject\wechat-monitor"))
WECHAT_PY = WECHAT_ROOT / ".venv" / "Scripts" / "python.exe"


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(data: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def run_digest(timeout: int = 1800, force: bool = False) -> dict:
    """调 wechat-monitor 处理当天文章，返回其 JSON 结果。"""
    if not WECHAT_PY.exists():
        return {"ok": False, "error": f"找不到 wechat-monitor 解释器: {WECHAT_PY}"}

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # 微信 / minimax 都走直连，避免代理干扰
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(k, None)

    argv = [str(WECHAT_PY), "-u", "-m", "wechat_monitor.cli", "digest"]
    if force:
        argv.append("--force")

    log.info(f"调用 digest: {' '.join(argv[1:])}")
    proc = subprocess.run(
        argv,
        cwd=str(WECHAT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )
    if proc.stderr.strip():
        for line in proc.stderr.strip().splitlines()[-12:]:
            log.info(f"  [digest] {line}")

    out = (proc.stdout or "").strip()
    if not out:
        return {"ok": False, "error": f"digest 无输出（exit={proc.returncode}）"}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"ok": False, "error": f"digest 输出不是 JSON: {out[:300]}"}


def build_markdown(r: dict) -> str:
    total = int(r.get("duration_s") or 0)
    mins, secs = divmod(total, 60)
    published = (r.get("published") or "")[:10]
    summary = (r.get("summary") or "").strip() or "（无摘要）"
    return (
        f"### {r.get('account') or '公众号'} · {published}\n\n"
        f"**{r.get('title') or '(无标题)'}**\n\n"
        f"{summary}\n\n"
        f"---\n\n"
        f"视频 {mins}:{secs:02d} ｜ 文字稿 {r.get('transcript_chars', 0)} 字 ｜ "
        f"耗时 {r.get('elapsed_s', 0)}s\n\n"
        f"[查看原文]({r.get('url', '')})"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略去重，强制重推")
    ap.add_argument("--dry-run", action="store_true", help="只处理不推送")
    args = ap.parse_args()

    today = date.today().isoformat()

    try:
        r = run_digest(force=args.force)
    except subprocess.TimeoutExpired:
        log.error("digest 超时（1800s）")
        return 1

    if r.get("skipped"):
        reason = r.get("reason", "")
        if "已处理过" in reason:
            log.warning(f"跳过（重复）：{reason}")
        else:
            log.info(f"跳过：{reason}（最近一篇 {r.get('latest')}）")
        return 0

    if not r.get("ok"):
        log.error(f"处理失败：{r.get('error')}")
        return 1

    log.info(
        f"处理完成：{r.get('title')} 视频 {r.get('duration_s')}s "
        f"文字 {r.get('transcript_chars')} 字 耗时 {r.get('elapsed_s')}s"
    )
    log.info(f"文字稿：{r.get('transcript_path')}")

    state = load_state()
    if not args.force and state.get("last_msgid") == r.get("msgid") and state.get("last_date") == today:
        log.info("今天这篇已经推过，跳过推送")
        return 0

    md = build_markdown(r)
    if args.dry_run:
        log.info("--dry-run，不推送。内容如下：\n" + md)
        return 0

    from tools.dingtalk_push import push_markdown

    ok = push_markdown(md, title=f"公众号日报 · {r.get('account') or ''}")
    if not ok:
        log.error("钉钉推送失败")
        return 1

    save_state(
        {
            "last_msgid": r.get("msgid"),
            "last_date": today,
            "last_title": r.get("title"),
            "last_pushed_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    log.info("已推送钉钉")
    return 0


if __name__ == "__main__":
    sys.exit(main())
