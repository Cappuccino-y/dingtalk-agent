# dingtalk-agent

钉钉 ↔ 本机 OpenCode agent 的桥接机器人。手机钉钉发一句话，本机的 OpenCode
接手（能跑命令、改文件、查资料、看行情），结果以钉钉 AI 卡片流式回显。

**这是一个独立仓库**，从 `D:\PythonProject\MainToy` 里抽出来的（详见
`docs/bot-architecture.md` 的「迁移说明」）。交易类脚本、定时作业、天勤账户
仍然留在 MainToy，本仓库不持有。

## 结构

```
bot/listen.py            入口：钉钉 Stream 回调、白名单、限速、指令、并发闸门
tools/opencode_bridge.py 桥接核心：会话/回合/事件流/卡片 v1/重启找回/模型切换
tools/card_v2.py         卡片 v2：公共模板 + 卡片 OpenAPI
tools/market_mcp.py      行情 MCP server（stdio）：quote/overseas/wave/tech/news
tools/wave_engine.py     道氏 + 艾略特波浪的确定性计算
tools/market_data.py     行情抓取（新浪）
tools/dingtalk_push.py   主动推送文本/图片/文件
tools/notifycation.py    钉钉 webhook / access_token
tools/bot_presence.py    停机三时点告知（重启前 / 掉线 / 已恢复）
tools/bot_restart.py     协同重启（只写标记，不 kill 自己）
datalayer/               baostock_daily / report_news_data / eastmoney_patch / trade_data_fetcher
scheduler/               调度器骨架（作业清单为空，作业在 MainToy）
.opencode-bot/           机器人专属 Location：AGENTS.md（行为规则）+ opencode.json
docs/bot-architecture.md 架构与维护手册（**改代码前先读**）
```

## 起来一次

```powershell
# 1) 配置（首次）
Copy-Item .env.example .env   # 然后填 APP_KEY / APP_SECRET / 模型 key 等

# 2) 前台跑（调试用；会和正式实例抢同一个钉钉连接，正式实例在跑时别开）
D:\Scoop\persist\anaconda3\App\envs\MainToy\python.exe bot\listen.py

# 3) 正式方式：交给看门狗
#    计划任务 dingtalk-bot-watchdog 每分钟跑一次
#    C:\Users\yydyy77\bin\dingtalk-bot-autostart.ps1
```

同一个 `APP_KEY` 只允许一条 Stream 连接，所以机器人有命名互斥体
（`Local\dingtalk_bot_v3`）防重复启动。

## 改完代码怎么生效

```powershell
python D:\PythonProject\dingtalk-agent\tools\bot_restart.py "改了 xxx"
```

**不要** `Stop-Process` / `taskkill` 去杀监听进程——那正是正在渲染你这条回复卡片的
进程，杀了会让钉钉那边永远停在「正在思考」。上面这条命令只写一个重启标记，
监听进程会在当前这一轮跑完、卡片收尾之后自行退出，看门狗再拉起（约 10 秒）。

## 验证

```powershell
& D:\Scoop\persist\anaconda3\App\envs\MainToy\python.exe -m compileall -q bot tools datalayer scheduler
Get-Content bot\logs\dingtalk_v3.log -Tail 50
Get-Content bot\logs\v3_stderr.log -Tail 30
python tools\bot_presence.py --status
python tools\bot_restart.py --status
```

## 注意

- `.env` 里有钉钉密钥和模型 key，**不进 git**。
- 行情数据依赖网络（新浪 / 东财 / 金十 / baostock），偶发超时属于正常，工具内部有降级。
- 卡片 v2 的硬限制（block 只认 0-3、单 block 约 3000 字、进度块必须单行）
  在 `docs/bot-architecture.md` 第四节，改卡片前务必先看。
