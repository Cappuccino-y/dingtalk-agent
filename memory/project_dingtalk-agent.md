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

---
memoryId: mem_488f97b4_77294
timestamp: 2026-10-20T06:21:34Z
scope: project:dingtalk-agent
category: decision
title: DingTalk Agent 是独立项目和独立 Git 仓库
tags: [evolve]
confidence: high
trust: self
source.messageIds: card:oc-88a1b0ef9f114f118367, session:ses_ea877a107ffe1IKS2gjnjD5Jgt, proposal:prop_20261020-140406_cbd8
source.agentIds: evolve, user-approved
---

触发条件: 维护代码、编写架构文档、配置依赖或说明部署边界时。
动作: 将 DingTalk Agent 作为独立项目和独立 Git 仓库维护，不将其描述或依赖为 MainToy、FindCap 下的子模块或原有同一项目。
适用范围: dingtalk-agent 的代码、文档、依赖、部署与仓库引用。

