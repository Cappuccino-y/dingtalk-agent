# 定时作业调度器（scheduler）

> 目标：把「时间表」从业务脚本里抽出来，长驻进程只服务外部事件流，
> 按时间跑的东西一律是「跑完就退」的短命进程。

## 一、为什么改

改造前的调度逻辑散在四个地方，彼此不知道对方存在：

| 位置 | 内容 | 问题 |
|---|---|---|
| Windows 计划任务 | autostart / watchdog / premarket | 改时间要开任务计划程序；注册点在系统里，代码里看不见 |
| autoworkflow.py | 一次性 fan-out 6 个子进程 + 15:00 关机 | 其中一条链路当天挂了（退出码 0xC000013A）也没人知道 |
| 脚本内部 | `while True` + `sleep(600)` + `wait_until(8,50)` | 把 cron 写进进程里：进程必须活到点，重启就漏跑 |
| 机器人进程内 | 定时线程 | 机器人一重启（协同重启会清上下文）就打断作业，职责也不对 |

四个地方各写一套去重（json 文件、min-interval、history）、各写一套日志、
各写一套推送，行为还不一致。结果就是：**当天有没有跑、跑成功没，只能翻目录**。

## 二、目标架构

```
                     ┌─────────────── 看门狗（计划任务，每 1 分钟）───────────────┐
                     │  dingtalk-bot-autostart.ps1                               │
                     │  谁不在拉谁：opencode 服务 → bot → scheduler               │
                     └───────────────┬───────────────────┬───────────────────────┘
                                     │                   │
                    ┌────────────────▼──────┐   ┌────────▼──────────────────────┐
                    │ bot（常驻·事件驱动）   │   │ scheduler（常驻·时间驱动）     │
                    │ 钉钉 Stream / 卡片     │   │ 只读调度表，不装 pandas        │
                    │ 纯交互，不跑定时任务   │   │ ~19 MB                         │
                    └───────────────────────┘   └────────┬──────────────────────┘
                                                          │ 到点 spawn（子进程）
                             ┌────────────────────────────┼────────────────────────┐
                             ▼                            ▼                        ▼
                    premarket 08:00              ic_monitor 08:50           ic_weekly 周一 09:05
                    （盘前早报，跑完就退）        （IC 信号，--once）        （周报 PNG）
                             │                            │                        │
                             └──────────► runs 表（SQLite）：状态/次数/耗时/日志尾巴 ┘
                                                  │
                                    失败 → notify.py（按 key 去重，只发一次）
```

**两个常驻进程**：bot（钉钉连接）和 scheduler（时间表）。
**N 个短命作业**：每个作业自己算完就退出，崩了也带不走调度器。
作业可以被单独手动跑，也可以换语言、换实现，调度器完全不关心。

触发源有三个，作业定义只有一份：定时（scheduler）、命令行（`run`）、
聊天（`/run`）。三者的区别只在记录里（trigger/actor），执行路径完全一致。

## 三、调度语义

每个作业每天最多一个 **occurrence**（计划时刻，如 `2026-10-21T08:50`），
它同时是去重键：

- **到点且仍在 `window_min` 窗口内** → 拉起；
- 已经 `success` → 永不重跑（重启、崩溃恢复都一样）；
- `failed` / `timeout` → 窗口内按 `retry_delay_s` 重试，最多 `max_attempts` 次；
- 进程跑到一半调度器被杀 → 下次启动把 stale `running` 标成 `orphan`，允许重试；
- 超过窗口还没成功 → 放弃并留记录（避免半夜补一条早报）。

这就是 cron 世界里常说的 misfire / coalescing：补跑要有窗口，重复执行要幂等。

## 四、目录

```
scheduler/
  config.py    路径、解释器、轮询间隔（SCHEDULER_HOME 可指向临时目录做测试）
  registry.py  作业注册表 + 时间计算（occurrence_for / next_due）
  db.py        SQLite：runs（运行记录）+ notifications（推送去重）
  runner.py    跑作业：子进程 + 超时杀进程树 + 落库 + 失败告警
  notify.py    调度层告警出口（走 tools.dingtalk_push，按 key 去重）
  daemon.py    主循环、单实例互斥、状态查询（status_text 给 /jobs 用）
  cli.py       python -m scheduler {serve,status,list,run,logs}
```

运行期数据放在 `~/.dingtalk-bot/`（不进仓库）：
`scheduler.db`、`scheduler.pid`、`logs/scheduler.log`、`logs/jobs/<作业>.log`。

### 作业脚本现在在哪

2026-10-20 机器人本体、调度器**和三个业务脚本**都搬到了本仓库，
`registry.py` 里的 `argv` / `cwd` 全部指向本仓库：

| 作业 | 脚本 | 依赖（也都在本仓库） |
|---|---|---|
| `premarket` | `jobs/premarket_push.py` | `tools/opencode_bridge.py`、`tools/dingtalk_push.py`、`tools/market_chart.py` |
| `ic_monitor` | `jobs/ic_signal_monitor.py` | `datalayer/akshare_helper.py`、`tools/notifycation.py` |
| `ic_weekly` | `jobs/ic_weekly_report.py` | 同上 |

运行期产物统一放 `var/`（`.gitignore` 已忽略）：

| 路径 | 内容 | 原来在哪 |
|---|---|---|
| `var/ic/` | IC 信号、最新信号、历史、每日去重状态、运行日志 | `MainToy/finanCalc/` |
| `var/weekly_reports/` | 周报 PNG | `MainToy/finanCalc/weekly_reports/` |
| `bot/bridge_data/` | 早报会话映射 + 推送时间戳 | `MainToy/finanCalc/bridge_data/` |

> 迁移时把老仓库的 `ic_latest_signal.json` / `ic_signal_history.json` /
> `premarket_last_sent.json` 一起搬过来了，所以**去重状态没断**——
> 迁移当天 IC 监控照常判定「今天已推过」而没重复推送。

> 顺带修掉一个老 bug：早报脚本原来把会话文件写到 `MainToy/finanCalc/bridge_data/`，
> 而机器人（`bot/listen.py` 的 `BRIDGE_DATA_DIR`）读的是 `bot/bridge_data/`——
> 两边对不上，机器人启动时那句「清空盘前会话」其实一直没生效。
> 现在两边统一到 `bot/bridge_data/`。

## 五、日常操作

```
python -m scheduler status             # 今天跑没跑、成功没、下次几点
python -m scheduler list               # 作业清单
python -m scheduler run ic_monitor     # 手动补跑一次（同步，看得到结果）
python -m scheduler run ic --dry-run   # 参数原样传给作业（别名也行）
python -m scheduler logs ic_monitor -n 60
python -m scheduler history            # 最近记录：定时/手动、谁点的、耗时
```

钉钉里发 **`/jobs`** 看同一份状态，**`/run <作业> [参数]`** 手动触发
（快捷 `/ic` `/早报` `/周报`）；跑完会单独推一条结果 + 日志尾巴。

手动与定时的关系（对应业界通行做法）：

| 维度 | 做法 |
|---|---|
| 实现 | 同一份 `runner.run_job`，手动只是换触发源 |
| 记录 | `runs.trigger` = scheduled / manual / chat，`actor` 记谁点的 |
| 去重键 | 手动用 `manual:2026-10-20T12:30:05`，不占定时那条 occurrence |
| 并发 | 同作业已有 running 就拒绝（等价 `concurrencyPolicy: Forbid`），跨进程查库判断 |
| 幂等 | 业务层去重不绕过：IC 当天推过就不再推，要重发显式 `--force` |
| 审计 | `history` 可查每次是定时还是谁手点的 |

## 六、新增一个作业

1. 在 `scheduler/registry.py` 的 `JOBS` 里加一条（`at` / `window_min` / `timeout_s`，
   可选 `aliases` 给聊天快捷名）；
2. 作业脚本自己实现成「跑一次就退出」，并保证幂等（同一天重复跑不会重复推送）；
3. 需要推送就用 `tools/dingtalk_push`，需要去重就用 `scheduler.notify.send(key=...)`。

不用注册计划任务，不用看门狗改代码——看门狗只认这两个常驻进程。

## 七、为什么不用 APScheduler

APScheduler 是业界标准做法（"一个应用里通常只该有一个 scheduler"），
但它擅长的是「进程活着时按 cron 触发」，补跑语义绑定在**持久化 jobstore +
misfire_grace_time** 上，而这里真正要的是**每个作业自己的业务窗口**
（早报 08:00 之后 3 小时内才有意义、IC 只在早盘 70 分钟内补）。
把两者拼起来仍要写一套"这个 occurrence 跑过没"的判断，收益不大。

所以这里用一个约 200 行的确定性循环 + SQLite 运行记录：
行为可读、可测（临时目录里跑，不碰真实库）、零新增依赖。
如果以后作业涨到几十个、需要分布式/多机，再换 APScheduler 或 Airflow 类方案。

## 八、范围与边界

- 只管**机器人自己**的作业：盘前早报、IC 信号监控、IC 周报。
- **不动** `autorun/autoworkflow.py` 那套（6 个子进程 + 收盘关机），它有自己的节奏。
- weclaw（微信桥）已整体删除，与本调度器无关。
- 计划任务只剩两条：`dingtalk-bot-autostart`（登录自启）与
  `dingtalk-bot-watchdog`（每 1 分钟看门狗）。原来的 `dingtalk-premarket` 已删除，
  早报改由 scheduler 的 `premarket` 作业负责。
