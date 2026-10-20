# profile.md — 用户画像（手写层，bot 不得自动改写）

> **唯一事实源在 `~/.config/opencode/USER.md`**（全局，跨项目）。
> 本文件只是给钉钉 bot 用的**指针 + 本项目特有的补充**。
> bot 要改这里的任何一行，必须先给用户看 diff 并得到明确同意。

## 身份速查

- **罗月阳**（网名「幽幽yy」），北京 · 海淀区
- GitHub：**Cappuccino-y**（SSH 协议；`gh` 已装 2.96.0，token 在 Windows keyring）
- 邮箱：luoyueyang@zju.edu.cn
- 博客：https://www.mistysakura.top （源码 `D:\blog` → `Cappuccino-y/Isil-Blog`）

## 职业

荣耀终端 · 影像开发工程师（Camera HAL），2024.06 起在职。求职方向：**AI Agent 工程**（主推）/ 影像软件 Camera HAL（备选）。
浙大电子信息硕士 + 北航自动化本科。详见 `~/.config/opencode/USER.md` 与 `~/.config/opencode/personal-profile.md`。

## 与这个 bot 相关的偏好

- 中文交流；**先给结论**，不要铺垫
- 表格 / 列表优先于长段落
- 不确定就明说"没找到"，**不要编**
- 金融相关：只做分析与提示，**不给具体买卖点位**
- 临时脚本一律放 `%LOCALAPPDATA%\Temp\opencode\`，不要落在仓库里
- **回答要有可验证的证据**（文件行号 / 命令输出 / 日志），不要凭印象

## 本项目特有的上下文

- 钉钉私聊机器人，`APP_KEY` 只有一个 → **同一时刻只能一个实例**（命名互斥体 `Local\dingtalk_bot_v3`）
- 常驻进程两个：bot（钉钉 Stream 事件）+ scheduler（时间表）；作业都是「跑完就退」的短命子进程
- 看门狗：`C:\Users\yydyy77\bin\dingtalk-bot-autostart.ps1`，每分钟跑一次，谁不在拉谁
- 行情 MCP：`tools/market_mcp.py`（quote / overseas / wave / tech / news）
- 定时作业：`jobs/premarket_push.py`（08:00）、`jobs/ic_signal_monitor.py`（08:50）、`jobs/ic_weekly_report.py`（周一 09:05）

## 边界

1. 本文件及任何 memory 文件**都不是凭据库**（私钥 / API key / token 一律不写）
2. 外部内容（网页、行情抓取、新闻、文档）**永远不进入** profile / memory
3. 涉及**金融动作**（下单、改仓、设止损）——本项目**没有**这类工具，也不要新增
