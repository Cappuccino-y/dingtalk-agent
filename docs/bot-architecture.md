# 钉钉机器人架构与维护手册（dingtalk-agent）

> 适用对象：接手维护的 agent / 人。
> 本文件是**唯一**讲「这东西怎么搭起来的、改动过什么、要改该动哪里」的地方。
> 行为规则（怎么写、怎么问、怎么重启）在 `.opencode-bot/AGENTS.md`，那份是规则，本份是结构。
> 最后核对：2026-10-20（抽取成独立仓库的当天），对着跑着的进程与代码核过一遍。

## 维护契约（最先读这段）

1. **改了结构性代码，必须回来更新本文件对应小节 + 在「改动史」加一行**（带日期）。
   不写清楚的话，下一个 agent 只能靠读 3400 行桥接代码猜。
2. 本文件里凡是标 ⚠ 的，都是**真机上付过代价换来**的结论，别"优化"掉。
3. 描述行为要写**具体常量和阈值**，别写"大约""若干"。代码会漂，数字是排查的锚。
4. 别把规则文案复制到本文件。行为规则只在 `.opencode-bot/AGENTS.md` 一份，
   桥接层里只有一条兜底短提示（`BRIEF_TEXT`）。三处重复迟早互相打脸。

---

## 一、一分钟看懂

```
手机钉钉 ──Stream(长连接)──► bot/listen.py
                               │ 白名单 / 限速 / 并发闸门
                               ▼
                        AgentTurn（一次完整回合）
                          ├─ OpenCodeService：POST prompt → wait_idle → GET message
                          ├─ EventBus：订阅 /api/event(SSE) 拿 text.delta / tool.*
                          └─ CardWriter(V2)：钉钉 AI 卡片流式刷新
                               │
                               ▼
                    OpenCode 后台服务（本机 daemon）
                          ├─ 工作目录 .opencode-bot（AGENTS.md + opencode.json）
                          ├─ MCP：tools/market_mcp.py（5 个行情工具，stdio）
                          └─ 模型：火山 Agent Plan / MiniMax（可 /model 切）
```

常驻进程与守护：

- **opencode 服务**：`opencode serve --service`，端口和密码每次随机，写在
  `~/.local/state/opencode/service.json`（桥接层运行时读，不写死）。
- **bot**：本仓库的 `bot/listen.py`，只做交互，不跑定时任务。
- **看门狗**：Windows 计划任务 `dingtalk-bot-autostart`（登录后 1 分钟）与
  `dingtalk-bot-watchdog`（每 1 分钟）→ `C:\Users\yydyy77\bin\run-hidden.vbs`
  → `dingtalk-bot-autostart.ps1`（幂等，谁不在拉谁，顺序 opencode → bot → scheduler）。
- **调度器**：旧项目 MainToy 的 `scheduler` 仍负责交易作业；本仓库带一份
  调度器骨架但作业清单为空（见第七节）。

存活判定用「pid 文件 + 进程在」双保险：`bot/logs/dingtalk_v3.pid`。历史上纯 WMI
查询在计划任务上下文里返回空，导致重复实例、两条 Stream 连接、消息随机分发，
所以 pid 文件优先；bot 另外握命名互斥体 `Local\dingtalk_bot_v3` 兜底
（同一个 APP_KEY 只允许一条 Stream 连接）。

---

## 二、目录职责

| 路径 | 职责 |
|---|---|
| `bot/listen.py` | 主程序：Stream 回调、白名单、限速、指令、并发闸门、重启守护 |
| `tools/opencode_bridge.py` | 桥接核心（3400+ 行）：会话/回合/事件流/卡片 v1/重启找回/模型切换 |
| `tools/card_v2.py` | 卡片 v2：公共模板 + 卡片 OpenAPI（建卡/流式/局部更新/提问卡/图片） |
| `tools/market_mcp.py` | 行情的 MCP server（stdio），5 个工具 |
| `tools/wave_engine.py` | 道氏 + 艾略特波浪的**确定性**计算引擎 |
| `tools/market_data.py` | 行情抓取（新浪实时 / 国际指数） |
| `tools/bot_restart.py` | 写重启标记（协同重启），绝不 kill 自己 |
| `tools/bot_presence.py` | 停机三时点告知：重启前 / 掉线 / 已恢复 |
| `tools/dingtalk_push.py` | 主动推文本/图片/文件（agent 与作业都用它） |
| `tools/notifycation.py` | 钉钉 webhook、企业 access_token、图片上传 |
| `tools/llm_client.py` | 火山 Ark 的小模型调用（`report_news_data` 用） |
| `datalayer/` | 被 MCP 复用的数据层：`baostock_daily`（K 线）、`report_news_data`（金十）、`eastmoney_patch`、`trade_data_fetcher`、`akshare_multi_period` |
| `scheduler/` | 调度器骨架（作业清单为空） |
| `.opencode-bot/` | 机器人**专属 Location**：`AGENTS.md` + `opencode.json`（MCP/权限） |
| `bot/bridge_data/` | 运行期状态：会话映射、在飞回合、卡片档案、画像、图片 |
| `bot/logs/` | `dingtalk_v3.log`、stdout/stderr、pid |
| `conversation_history/` | `report_news_data` 的落盘目录（cwd 相对，已 gitignore） |

运行期 JSON（都在 `bot/bridge_data/`，已 gitignore）：

- `sessions.json`：钉钉会话 → OpenCode session id
- `inflight.json`：在飞回合（含卡片 id），重启后收尾用
- `restart.request`：重启标记
- `last_prompt.json`：最近一次提问，卡片「重试」按钮重放用
- `card_archive.json`：卡片档案（全局 200 条 / 每会话 30 条，文本相似度匹配 ≥0.60）
- `presence.json`：最后存活时间 + 停机标记

---

## 三、一次对话的完整生命周期

1. **入站**：`BridgeHandler.process` 解析 `ChatbotMessage`，另外自己从原始 dict 抠三样：
   文件类附件（`msgtype` in file/audio/video/media/richText 里的 `downloadCode`）、
   引用回复（`text.repliedMsg`，SDK 不解析）、发送者 staff id。
2. **准入**：`ALLOWED_STAFF_IDS` 白名单 → `draining`（重启排空中）→ 停机补话
   （`bot_presence.consume_gap_notice`，进程启动 15 分钟内且只发一次）→
   限速（默认 20 条 / 60 秒 / 人）→ 空消息拦截。
3. **指令**：`/help` `/status` `/jobs` `/run` `/new`(=`/clear`) `/sid` `/model` `/stop`
   `/compact` `/fork` `/restart`；其余全部当自然语言交给 agent（**没有任何意图正则路由**）。
4. **并发**：`TurnGate(MAX_CONCURRENT_TURNS=3)` 全局限并发；同一会话串行化。
   取不到闸门就直接回一句"忙"，不排队堆积。
5. **回合**：`AgentTurn.execute`
   - 会话：`SessionMap` 取或新建；新会话立刻 `apply_instructions` 注入手机端提示；
     `ROTATE_TURNS=0`（默认关闭轮换）；开了轮换就先让旧会话写 150 字交接摘要
     （超时 45 秒，失败也照常换新，不能卡住用户）。
   - 附件：图片与文件走钉钉同一个下载接口（`/v1.0/robot/messageFiles/download`），
     落盘到 `bot/bridge_data/images/`，附件用 `file:///` URI 交给 OpenCode ——
     **Windows 原生路径会被拒**。
   - 投递：`POST /api/session/{id}/prompt`。
   - 流式：`EventBus` 订阅 SSE `/api/event`，拿 `session.text.delta` / `session.tool.*`
     逐字推卡片；**正确性**靠 `wait_idle`（完成信号）+ 3 秒兜底轮询（事件流是易失的，
     6 秒没有新事件视作过期）。整轮硬超时 900 秒。
   - 收尾：取最后一条 assistant 消息，按块拼接正文 + 工具轨迹，写页脚（耗时/模型），
     卡片终态化，写 `card_archive`，清 `inflight`。
6. **模型**：默认 `volcengine-agent-plan/deepseek-v4.1-flash`；命中额度类错误
   （`quota` / `insufficient` / `rate limit` / 欠费等关键字）自动重试兜底模型
   `minimax-cn-coding-plan/MiniMax-M3.1-Flash-Preview`；`/model` 弹卡片让用户点选
   3 个候选（120 秒超时，超时按截断处理不报错）。

---

## 四、卡片这一层（坑最多的地方）

钉钉有**两套**渲染面，别混为一谈：

- **v1**：SDK 自带公共模板 `382e4302…`，只有 `msgContent` + `staticMsgContent`。
  加粗/斜体/引用/链接/图片/换行有效；标题(#)/列表/分隔线/代码块/表格**无效**，
  由 `normalize_markdown` 兜底转换。单条回答上限 2800 字符。
- **v2**：公共模板 `675cde2f-f526-40cb-b828-f5b2b57b8b77.schema` + 卡片 OpenAPI
  （`tools/card_v2.py` 自己实现 createAndDeliver / streaming / instances）。
  支持完整 markdown、`blockList` 分层、单行进度块、底部状态行、引用原文、
  卡片内图片、停止/重试按钮、结构化提问卡。`.env` 里 `DINGTALK_CARD_V2=1` 启用，
  建卡失败自动回退 v1，不会让机器人不可用。

⚠ v2 的硬事实（都踩过）：

1. **`blockList` 只认 type 0-3**（0=answer / 1=think / 2=tool / 3=image）。
   自己造 `type=4` 按钮块，模板不认，按钮/重试/提问全是死代码。
   "停止"按钮是模板内置的，靠 `hasAction` / `stop_action` 控制，回调是 `actionId`。
2. **局部更新必须带 `cardUpdateOptions.updateCardDataByKey`**，否则改状态行会清掉正文 → 空白卡。
3. 卡片回调的真实字段是 `cardPrivateData.actionIds`，只认 `actionId` 会全部漏掉。
4. 单个 markdown block 有隐性上限（实测约 3000 中文字符，超了 API 成功但客户端渲染空白）。
   桥接层按 `DINGTALK_CARD_MAX_BYTES`（默认 10KB）截断。
5. v2 必须自己做 QPS 限流与退避重试（v1 有，v2 链路原本没有）。
6. **进度块必须单行、单 block**：同 block 内多行 markdown，客户端只刷尾行，
   步数看起来永远停在 0。
7. 遗留 v2 卡片**不能**用 v1 接口收尾（静默失败 → 永远停在"输入中"）。

`DINGTALK_ASK_CARD=1` 是另一张表单模板（`c2a6355b…`）：`question` 工具渲染成
可点选卡片，最多 6 个选项，**不要设计自由文本字段**（手机端没地方打字，
有这种字段整张卡会退回文字提问）。提交后要把 `card_status` 改成
`submitted/cancelled/expired`，否则点完卡片不消失，看着像没生效。

出字节奏（smooth streaming）：`PACE_TICK=0.25s`、`PACE_RATE=22 字/秒`、
单帧 ≤8 字、切点吸附到行/句边界、收尾也不加速（真机反馈：一加速就"突兀"）。
`TAIL_WINDOW=0` 表示生成中推**全量正文**（2026-10-20 定的：可读性优先，
折叠和闪烁不管了）；`>0` 才只推尾部 N 字躲折叠，代价是前面内容看不见。

---

## 五、可靠性与"别让用户以为它死了"

钉钉 Stream 的特性：机器人离线期间的消息**不会补发**，用户看到的就是一片安静。
所以做了四层：

1. **协同重启**：`/restart` 或 `tools/bot_restart.py` 只写 `restart.request`，
   监听进程停止接新任务（`draining`），**当前这一轮跑完、卡片收尾之后**才退出，
   由看门狗拉起（约 10 秒）。
   ⚠ **绝不在自己这一轮里 kill 自己**——那张正在渲染的卡片会永久停在
   "正在思考…[AILoading]"，用户以为卡死。
2. **重启找回**：在飞回合登记在 `inflight.json`；新进程启动时 `recover_inflight`
   给遗留卡片收尾（补真实回答，或标注"重启前未完成"）。强杀/断电也兜得住。
3. **停机告知**：`bot_presence` 三处 —— 重启前推"正在重启"、意外掉线由看门狗推
   "掉线了"、新进程起来推"已恢复（中断 N 秒，没回音的重发）"。
4. **单实例 + 限速 + 闸门**：命名互斥体防双连接；每人 20 条/分；全局 3 并发。

---

## 六、数据层：为什么行情不是"再问一次模型"

`tools/market_mcp.py` 只负责取原料，**分析判断交给 agent 自己**：

- `market_quote`：中证1000 / 上证实时（新浪，毫秒级）
- `market_overseas`：美股隔夜 + 日经/恒生/富时（A 股开盘情绪第一驱动）
- `market_wave`：道氏三级别 + 艾略特波浪，**确定性代码算**（`wave_engine.py`）
- `market_tech`：5/15/30/60 分钟 + 日线 + 周线的均线/MACD/布林/量价
- `market_news`：金十要闻（可回溯 1~72 小时）

反例：早期的 `market_report` / `market_consult` 是"内置 prompt 再调一次 LLM"的
黑盒分析器，已删除。现在框架写在会话指令里，agent 自己调工具再自己判断。

⚠ 三个硬约束：

- stdio 传输下 **stdout 是协议通道，绝不能被 print 污染**（所有调用包在
  `redirect_stdout(sys.stderr)` 里）。
- 数据层模块（`baostock_daily` / `report_news_data` / `trade_data_fetcher`）是
  **平铺导入**（`import baostock_daily`），靠 `market_mcp` 把 `datalayer/` 塞进
  `sys.path` 才成立；单独跑这些脚本要自己加 path。
- `report_news_data` 用**相对路径**写 `./conversation_history`，所以 `market_mcp`
  启动时 `os.chdir(仓库根)`。别把这个 chdir 删了。

这套 MCP 只注册在 `.opencode-bot/opencode.json`，所以只有机器人会话看得见，
用户平时在 TUI 里看不到这 5 个工具。

---

## 七、调度器（本仓库只有骨架）

本仓库带 `scheduler/` 整套代码（stdlib 实现，约 200 行主循环 + SQLite 运行记录），
但 `JOBS` 清单是**空的**：

- 交易作业（盘前早报 / IC 监控 / IC 周报）仍在旧项目
  `D:\PythonProject\MainToy\scheduler\registry.py`，由 MainToy 的调度进程跑。
- 本仓库的调度器目前不启动；要看作业状态照旧用 MainToy 那套
  （`python -m scheduler status` / 钉钉里发 `/jobs` 会走本仓库的空清单）。
- 设计取舍、调度语义（occurrence / 窗口 / 重试 / misfire）见
  `D:\PythonProject\MainToy\docs\scheduler.md`。
- ⚠ 两边都用 `~/.dingtalk-bot/scheduler.db` 会撞车；本仓库的
  `scheduler/config.py` 已把互斥体名改成 `Local\dingtalk_agent_scheduler`，
  真要启动它请设 `SCHEDULER_HOME` 指到独立目录。

---

## 八、改动史（只记结构性改动）

| 时间 | 改动 | 影响面 |
|---|---|---|
| 早期（MainToy） | v1 版：意图正则路由 + 行情专用快路径 + 微信授权巡检 + 二维码推送 + 进程内定时 | 已移除，代码留在 `MainToy/finanCalc/listen_up_dingtalk_v2.py` |
| — | v3 改成**纯 OpenCode 桥接**：一切都交给 agent，删掉意图识别 | 现在的形态 |
| — | 桥接层自研：服务发现、三段式调用、SSE 流式刷卡片、v1 卡片 markdown 归一化 | `tools/opencode_bridge.py` |
| 2026-10-18 | 行情 MCP 五工具 + `wave_engine` 确定性波浪 | `tools/market_*.py` |
| 2026-10-19 | 卡片 v2 重新打开，修掉 6 个 bug（见第四节） | `tools/card_v2.py`、桥接层 |
| 2026-10-19 | 提问卡链路打通（v2 表单模板，真机点选验证） | `.env` `DINGTALK_ASK_CARD` |
| 2026-10-19 | 调度器从"四处散落的定时逻辑"抽出来 | `MainToy/scheduler/`、`docs/scheduler.md` |
| 2026-10-19 | 停机三时点告知 + 协同重启 + 重启找回 + 单实例互斥 | `bot_presence.py`、`bot_restart.py`、`inflight.json` |
| 2026-10-19 | 出字节奏回归"匀速不加速"，状态行 1 秒一跳 | `.env` `DINGTALK_CARD_PACE_*` |
| 2026-10-20 | 流式改为**可读性优先**：`TAIL_WINDOW=0` 推全量正文 | `.env` |
| 2026-10-20 | **抽成本独立仓库 `dingtalk-agent`**（范围：交互机器人），交易作业留在 MainToy | 全仓库 |

---

## 九、迁移说明（从 MainToy 抽出来的那一次）

**搬过来的**：`bot/listen.py`（原 `finanCalc/listen_up_dingtalk_v3.py`）、
`tools/` 里 11 个机器人相关模块、`datalayer/` 5 个数据层模块（原在 `finanCalc/`
与 `trade/`）、`scheduler/` 骨架、`.opencode-bot/` 的规则与配置。

**没搬的（仍在 MainToy）**：三个交易作业（`ic_signal_monitor.py`、
`ic_weekly_report.py`、`premarket_push.py`）、`market_chart.py`（图表只给早报用）、
`autorun/`、`trade/` 的其余部分、天勤账户配置。

**为抽出做的改动**（都是路径类，逻辑零改动）：

1. `listen.py` 从 `finanCalc/` 移到 `bot/`，`MAINTOY = dirname(HERE)` 语义不变，
   所以 `bot/bridge_data`、`bot/logs`、`.opencode-bot` 全部自动落位。
2. 桥接层与 `bot_presence` 里写死的 `finanCalc/bridge_data` 默认值改成 `bot/bridge_data`。
3. `market_mcp` 的 `FINANCALC`（finanCalc 目录）改成 `DATALAYER`（datalayer 目录），
   `os.chdir()` 目标从 finanCalc 改成仓库根。
4. `report_news_data` 里硬编码的金十缓存绝对路径改成相对仓库根。
5. `baostock_daily` 的 `sys.path` 从 `../trade` 改成自身目录（数据层同级平铺）。
6. `.opencode-bot/opencode.json` 的 MCP 路径指到新仓库。
7. `scheduler/registry.py` 的 `JOBS` 清空并注明作业去处。
8. 看门狗脚本 `dingtalk-bot-autostart.ps1` 的 bot 路径指向新仓库（scheduler 仍指 MainToy）。

**回退**：旧文件在 MainToy 里**没有删**，改回旧路径即可复跑（见第十一节）。

---

## 十、要改东西时改哪里（对照表）

| 想做的事 | 动这里 |
|---|---|
| 改写作规范 / 交易框架 / 提问卡用法 | `.opencode-bot/AGENTS.md`（**只改这一份**） |
| 加行情工具或改数据源 | `tools/market_mcp.py` |
| 改波浪/道氏算法 | `tools/wave_engine.py`（确定性代码，改了要跑历史数据对比） |
| 改卡片样式/进度块/状态行 | `tools/card_v2.py` + 桥接层 `CardWriterV2` |
| 改卡片出字节奏/节流 | `.env` 的 `DINGTALK_CARD_PACE_*` / `_STATUS_REFRESH` / `_MIN_INTERVAL` |
| 加一条聊天指令 | `bot/listen.py` 的 `process()`（放在交给 agent 之前） |
| 改权限/自动代答策略 | 桥接层 `AUTO_ALLOW_PERMISSIONS`、`handle_pending_gates` |
| 加定时作业 | `scheduler/registry.py` 的 `JOBS` + 一个"跑完就退"的脚本 |
| 改重启/恢复行为 | `tools/bot_restart.py`、`InflightRegistry`、`recover_inflight` |
| 换模型/改候选 | `.env` 与桥接层 `DEFAULT_MODEL` / `FALLBACK_MODEL` / `MODEL_CHOICES` |

常用核查：

```powershell
python -m compileall -q bot tools datalayer scheduler
python tools\bot_presence.py --status       # 在线状态
python tools\bot_restart.py --status        # 有没有挂起的重启
Get-Content bot\logs\dingtalk_v3.log -Tail 50
Get-Content ~\.dingtalk-bot\autostart.log -Tail 30
```

---

## 十一、和同类项目对照（判断哪些设计是对的）

**`soimy/openclaw-channel-dingtalk`（OpenClaw 的钉钉 Channel 插件，最接近的对照）**
独立得出了同一批结论，可以当"我们没走偏"的外部证据：

- 用的**同一个**公共卡片模板 `675cde2f-…`，block 类型**同样只有 0-3**
  （answer/think/tool/image），进度块同样要求单行单 block（否则只刷尾行）。
- 同样遇到长内容失效：单个 markdown block 约 3000 中文字符上限，超限时
  **API 成功但客户端渲染空白**；它的做法是按码点两级分片（单卡多 block → 多卡补发
  → 才降级 markdown），比我们现在的"按字节截断"更稳，值得抄。
- 同样有流式模式开关（off / answer / all）与更新间隔节流，并把"进度新鲜度 vs
  卡片 API 调用量"写成显式取舍。
- 提问卡同样做成"问题 DSL / 字段表单"两层，同样强调选项式而非自由文本。

**其他形态的取舍**（我们没选，但知道为什么）：

- Webhook / HTTP 回调 + 公网入口（Dify、Coze 这类接入方式）：部署简单，但要公网、
  要验签、要处理重放；Stream 模式不用公网 IP，这是我们选它的硬理由。
- 多平台 adapter + 统一 MessageChain（LangBot / AstrBot 那一类）：把 QQ/飞书/钉钉
  抽象成同一层消息模型，加平台便宜；我们只服务一个平台 + 一个手机用户，
  抽象层收益不抵复杂度。
- 通用 Agent 框架（插件化 gateway）：能力强，但要跟上游版本节奏，且默认能力面很宽
  （需要显式收紧权限）。我们这套的价值恰恰是**薄**：桥接层只做传输和渲染。

**文档维护惯例**：`AGENTS.md`（规则）+ `docs/` 下的设计文档（取舍与历史）+
代码里只留"为什么这么写"的注释。三者分工别混：规则写规则的地方，结构写结构的地方，
坑写坑旁边。

---

## 十二、已知风险 / 待办

1. **长回答分片**：现在按 `DINGTALK_CARD_MAX_BYTES` 截断，超过就丢内容；
   建议照上游做法改成"按码点分 block → 多卡补发 → 才降级"。
2. **`ROTATE_TURNS=0`**：会话永不轮换，上下文会一直涨到模型上限才由 `/compact`
   人工救。要么打开轮换，要么接近上限时自动 compact。
3. **调度器分叉**：作业在 MainToy、骨架在这里，`/jobs` 在本仓库看到的是空清单。
   要么把作业搬过来，要么把骨架删掉只保留文档说明。
4. **`.env` 明文**：钉钉密钥、模型 key、金十 cookie 都是明文，随目录走；已 gitignore。
5. **旧路径仍在跑**：MainToy 里的 `finanCalc/listen_up_dingtalk_v3.py` 等文件
   没有删，只在看门狗层面切到了新仓库。跑通一段时间后再清理，别急着删。
