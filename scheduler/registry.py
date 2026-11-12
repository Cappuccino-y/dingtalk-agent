"""作业注册表：想加一个定时任务，只在这里加一条。

约定（很重要）：
- 每个作业都是「跑完就退」的短命进程，自己算完就退出，**不要在里面写 while+sleep**；
- 作业必须幂等：同一个 occurrence（计划时刻）重复执行也不能重复推送；
- 时间窗口 window_min 表示「错过之后还能补跑多久」，超出就不再补（避免半夜补一条早报）。

occurrence 就是「这次该在哪一刻跑」，也是去重键：
    2026-10-21T08:00   ← premarket 在 10-21 的计划时刻

**IC 监控（抄底/顶部信号）归本 bot**（2026-10-23 从 MainToy 那条链收回）：
用户明确「这个 icmonitor 完全由你控制」→ 启动、状态、去重、推送都归本仓库：
- 定时：本仓库 `ic_monitor`(08:50) / `ic_recheck`(09:20) / `ic_weekly`(周一 09:05)
- 脚本：`jobs/ic_signal_monitor.py`（状态/历史在 `var/ic/`，已并入 MainToy 那条链的历史）
- 推送：**本机器人 1:1**（tools.dingtalk_push）→ 署名本机器人，不是「py 小助手」群机器人
- `MainToy\autorun\autoworkflow.py` 已不再拉起 IC 脚本 → 不会双推
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from .config import IC_SCRIPT, IC_WEEKLY_SCRIPT, PYTHON, REPO

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


# bot 自己的作业。IC 监控/补判/周报 2026-10-23 起收回本 bot（见模块头注释）：
# 定时、状态、去重、推送全部由本仓库负责。
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
    # 产业要闻早报（2026-11-11 新增）：多源快讯 → MiniMax 通读 → 板块级要点。
    # 为什么和 premarket 分开：早报是 agent 现场算点位/波浪，消息面只顺带一句；
    # 这条专门盯消息面 —— 像「微软游说白宫放弃中国光模块进口禁令」这种海外政策
    # 传导到 A 股板块的消息（东财 08:36、富途 12:25 都有），早报整条漏掉。
    # 口径：板块区只谈板块不点名；**个股重大利好单列一区**（2026-11-12 用户改口径：
    # 「像个股的重大利好可以做推送」—— 只限有明确重大催化的，不写买卖建议）。
    # ⚠ 2026-11-12：脚本内回溯窗口 12h → 25h。这作业一天只跑一次，
    # 12h 窗口会让「前一交易日盘中到盘后」那 12h 永远抓不到（盲区，
    # 实测把超颖电子 11-11 16:03 的 IR 记录漏掉了）。窗口数在脚本里
    # （jobs/news_digest.py::DEFAULT_HOURS），这里不重复写。
    Job(
        id="news_digest",
        name="产业要闻早报",
        argv=[PYTHON, os.path.join(REPO, "jobs", "news_digest.py")],
        at=(8, 35),
        window_min=150,          # 08:35~11:05，开机晚也能补
        dow=None,                # 每天（周末/假期也有海外消息）
        # timeout_s 1500 → 2400：窗口扩到 25h 后候选 ~3100 条（12h 是 ~1500），
        # 抓取 ~120s（12h 是 ~15s）+ 挑选 ~8 分钟（300 条一批，9 批 × 3 并发）
        # + 终稿一轮。实测 25h 全程 ~15 分钟，1500s 会半路被杀。
        # ⚠ 上限不能无限放宽：08:50 是 IC 监控的判定时刻，早报不能跟它抢配额，
        # 所以脚本内部还有一道「08:50 前必须收尾」的软闸。
        timeout_s=2400,
        max_attempts=2,
        retry_delay_s=300,
        notify_on_fail=True,
        aliases=("要闻", "新闻", "news"),
        cwd=REPO,
    ),
    # 产业要闻**午间版**（2026-11-12 用户要求：「目前不仅是早上推，中午也推……
    # 在 12:55 的时候要推送出来，不是 12:55 才开始推送」）。
    #
    # **投递时刻 ≠ 启动时刻**：这一轮 12:20 起跑，抓取+挑选+定稿约 10 分钟，
    # 内容备好后**等到 12:55 再推**（脚本内 `--slot noon` + `_wait_until` 实现）。
    # 12:55 正好在午休结束（13:00 开盘）前，读者在下午开盘前能看到上午的消息面。
    #
    # 窗口只覆盖「早报推送之后」那几小时（上午盘中）—— 不重复早报的内容，
    # 也补上早报结构性看不到的**盘中异动**和**午间公告**。
    Job(
        id="news_digest_noon",
        name="产业要闻午间版",
        argv=[PYTHON, os.path.join(REPO, "jobs", "news_digest.py"), "--slot", "noon"],
        at=(12, 20),
        window_min=60,          # 12:20~13:20，晚开机也能补（补跑时过了 12:55 就直接推）
        dow=None,               # 每天
        # timeout_s 2700（45 分钟）：12:20 起跑 + 挑选 ~10 分钟 + 等到 12:55 投递。
        timeout_s=2700,
        max_attempts=2,
        retry_delay_s=300,      # 12:20 失败 → 12:25 重试，仍赶得上 12:55
        notify_on_fail=True,
        aliases=("午间", "午间要闻", "午报"),
        cwd=REPO,
    ),
    # IC 信号监控：**归本 bot**（2026-10-23 从 MainToy 那条链收回）。
    # 08:50 主判：触发就推信号，没触发就推每日心跳日报。
    # 2026-10-24：**周末也跑**（用户要求）—— 周末 akshare 最新数据 = 周五收盘，
    # 周末这一轮是第一次判到周五收盘数据，推「非交易日回顾」；脚本内部按数据日去重。
    # 去重靠脚本自己的历史文件（var/ic/ic_signal_history.json），
    # 所以 09:20 补判、手动 /ic 都不会把同一条信号推两遍。
    Job(
        id="ic_monitor",
        name="IC 信号监控",
        argv=[PYTHON, IC_SCRIPT, "--once"],
        at=(8, 50),
        window_min=180,          # 2026-10-25：70 → 180（08:50~11:50）
                                  # 机器晚开机也能补上（10-23 调度器 11:54 才起来，
                                  # 早报那轮还在窗口内，IC 那轮已经错过 → 中午只能手动补）
        dow=None,                # 每天都跑（周末跑非交易日回顾）
        timeout_s=600,
        max_attempts=3,
        retry_delay_s=180,
        notify_on_fail=True,     # 失败往本机器人 1:1 推告警
        aliases=("ic", "IC监控", "IC信号"),
        cwd=REPO,
    ),
    # 09:20 补判：08:50 那轮若因数据滞后被拒判（数据完整性守门），
    # 这里再判一次，只补「真信号」不发心跳（--triggers-only）。
    # 之前 MainToy 那条链是常驻进程每 10 分钟复查到 10:00；这里用一次补判等价收敛。
    # 2026-10-25：dow 改成**每天** —— QVIX 这类源补齐没有固定时间（实测能差到下午），
    # 周末/节假日同样可能补齐，当天就该把真信号推出来。
    Job(
        id="ic_recheck",
        name="IC 信号补判",
        argv=[PYTHON, IC_SCRIPT, "--once", "--triggers-only"],
        at=(9, 20),
        window_min=40,           # 9:20~10:00
        dow=None,                # 每天都补判（原来只周一~周五）
        timeout_s=600,
        max_attempts=2,
        retry_delay_s=180,
        notify_on_fail=True,
        cwd=REPO,
    ),
    # IC 周报：周一 09:05 出图并推 1:1（用同一份收盘数据）。
    Job(
        id="ic_weekly",
        name="IC 周报",
        argv=[PYTHON, IC_WEEKLY_SCRIPT],
        at=(9, 5),               # 排在 IC 监控之后，用的是同一份收盘数据
        window_min=100,
        dow=(0,),                # 只在周一
        timeout_s=900,
        max_attempts=2,
        retry_delay_s=300,
        notify_on_fail=True,
        aliases=("周报", "weekly"),
        cwd=REPO,
    ),
    # QVIX 收盘快照：15:05 把当天 QVIX 存到本地（var/qvix/），不再等 optbbs 回填。
    # 2026-10-26 起：源站 10-23/10-24 连续两天没回填 → IC 信号「拒判」，见改动史。
    # 15:05 是为了等 15:00 那根收盘分钟值写进 vix500.csv（作业内部也会卡 15:00）。
    Job(
        id="qvix_snapshot",
        name="QVIX 收盘快照",
        argv=[PYTHON, os.path.join(REPO, "jobs", "qvix_snapshot.py")],
        at=(15, 5),
        window_min=120,          # 15:05~17:05，机器晚点亮也能补
        dow=WEEKDAYS,
        timeout_s=300,
        # 2026-11-03：从 3 次/120s 提到 8 次/600s（覆盖到 ~16:25）。
        # 原来的 6 分钟根本不够 —— 源站是「事后回填」，15:05 就说没算完很常见；
        # 加上现在分钟文件没更新会**拒绝落库**（判源站未更新），更得留足重试空间。
        max_attempts=8,
        retry_delay_s=600,
        notify_on_fail=True,     # 500ETF 落不到 = 明天信号缺数据，得知道
        aliases=("qvix", "QVIX快照"),
        cwd=REPO,
    ),
    # QVIX 晚间补采（2026-11-09）：源站会整天不更新（11-09 分钟文件还是 11-06 那份），
    # 15:05~16:25 那 8 次怎么重试都拿不到。官方「期权风险指标」是傍晚才发，所以再开一轮：
    #   先照常抓快照（源站 17 点后回填的话这一轮能落**实测值**），
    #   抓不到就用「官方 IV 自算 + 锚定链式」补缺口（--proxy-fill，标 source=proxy），
    # 保证次日 08:50 的 IC 判据不会因为缺一天整轮拒判。窗口因此从 16:25 实际延到 23:00。
    Job(
        id="qvix_evening",
        name="QVIX 晚间补采",
        argv=[PYTHON, os.path.join(REPO, "jobs", "qvix_snapshot.py"), "--proxy-fill"],
        at=(20, 0),
        window_min=180,          # 20:00~23:00：机器开着就补
        dow=WEEKDAYS,
        timeout_s=420,
        max_attempts=8,          # 20:00 起每 10 分钟一次（到 ~21:30），官方数据发的晚也能等
        retry_delay_s=600,
        notify_on_fail=True,
        aliases=("qvix补采", "QVIX晚间"),
        cwd=REPO,
    ),
    Job(
        id="evolve",
        name="知识与行为改进扫描",
        # 读 journal + 记忆索引 + 问答存档 -> LLM 提提案 -> 推钉钉审批卡。
        # 提案者直连 LLM（无工具），落地由 tools/apply_proposal.py 确定性完成。
        argv=[PYTHON, os.path.join(REPO, "tools", "evolve.py")],
        # 这台机器每天 08:40 才开机，凌晨 3 点永远跑不到（窗口过了就永久跳过）。
        # 2026-10-29 从 08:00 挪到 10:00：
        #   08:00 的 occurrence 会在开机瞬间（08:41）和 premarket 一起被补跑，
        #   那时代理/网络还没起来，两个模型全 Connection error（当天白跑一次）。
        #   挪到 10:00 = 开机后网络早稳定，也错开 08:50 IC / 09:05 周报 / 09:20 补判。
        #   窗口仍到 20:00：当天开机晚也能补跑一次。
        at=(10, 0),
        window_min=600,           # 10:00~20:00 之间机器开着都会补跑
        dow=None,                 # 每天都跑（周末也有新交互）
        timeout_s=900,
        max_attempts=2,           # 开机瞬间断网这种偶发失败，隔 10 分钟补一次
        retry_delay_s=600,
        notify_on_fail=True,
        aliases=("改进", "复盘", "evolve"),
        cwd=REPO,
    ),
    # 公众号视频日报：每天 17:00 检查「远道擒龙」当天有没有发复盘视频，
    # 有就 下载 -> ASR 转文字 -> MiniMax 提炼要点 -> 推钉钉 1:1。
    # 重活在 D:\PythonProject\wechat-monitor（独立 venv + faster-whisper + CUDA），
    # 本作业只做调度和推送，用 subprocess 隔离，不往本仓库装 torch。
    # 17:00 是因为该号固定在 15:57~16:06 发布，留一小时余量；
    # 当天没发就正常退出，不推空消息（详见 jobs/wechat_video_digest.py）。
    Job(
        id="wechat_video",
        name="公众号视频日报",
        argv=[PYTHON, os.path.join(REPO, "jobs", "wechat_video_digest.py")],
        at=(17, 0),
        window_min=180,          # 17:00~20:00，机器晚开机也能补
        dow=None,                # 每天都跑（这个号节假日也可能更新）
        timeout_s=1800,          # ASR 单篇 3~5 分钟，留足余量
        max_attempts=1,          # 重跑要重新转写，代价高；脚本自己按 msgid 去重
        notify_on_fail=True,
        aliases=("公众号", "视频日报", "wechat"),
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
