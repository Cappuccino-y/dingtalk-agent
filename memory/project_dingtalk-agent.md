---
memoryId: mem_8f40bd96_75263
timestamp: 2026-10-20T05:47:43Z
scope: project:dingtalk-agent
category: gotcha
title: 重启/断连期间需主动推送状态提示
tags: [evolve]
confidence: high
trust: self
source.messageIds: -
source.agentIds: evolve, user-approved
---

触发条件: 机器人重启期间或断连后用户发送消息
动作: 给用户推送一条状态提示（如「正在重启中，请稍候」或「已恢复」），而不是静默丢弃消息
适用范围: 钉钉机器人重启、网络断开重连场景

---
memoryId: mem_25fe8d2e_77293
timestamp: 2026-10-20T06:21:33Z
scope: project:dingtalk-agent
category: preference
title: Bot 作业采用统一架构并同时支持定时与手动执行
tags: [convention, approved]
confidence: high
trust: self
source.messageIds: card:oc-f33718de48364d5d8602, session:ses_ea8c6454cffeLUw1aLr5o8wBPN, proposal:prop_20261020-140406_6652
source.agentIds: evolve, user-approved
---

触发条件: 为 Bot 接入工具、后台作业或定时任务。
动作: 按统一架构重构，避免每个工具单独设置常驻守护进程；jobs 除定时执行外，也要支持用户手动触发。
适用范围: dingtalk-agent 的 Bot、scheduler、工具和作业执行体系。

