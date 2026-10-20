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

