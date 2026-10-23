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

---
memoryId: mem_0406f975_09403
timestamp: 2026-10-21T19:03:23Z
scope: project:dingtalk-agent
category: decision
title: 流式回复不得因避免展开而截断内容
tags: [evolve]
confidence: high
trust: self
source.messageIds: journal:2026-10-20T02:23:00, proposal:prop_20261022-030300_c0dd
source.agentIds: evolve, user-approved
---

触发条件: 流式更新中的回复超过显示限度，出现“展开”或持续刷新。
动作: 不再为了规避展开而限制或截断可见回复；允许刷新或展开，优先保证回复内容完整可读。
适用范围: dingtalk-agent 的流式回复展示。

---
memoryId: mem_898128f0_09422
timestamp: 2026-10-21T19:03:42Z
scope: project:dingtalk-agent
category: preference
title: 结构性改动需同步维护 Bot 架构文档
tags: [convention, approved]
confidence: high
trust: self
source.messageIds: journal:2026-10-20T12:23:03, proposal:prop_20261022-030302_0b0e
source.agentIds: evolve, user-approved
---

触发条件: 修改 Bot 的进程架构、作业调度、目录边界、框架或其他结构性代码。
动作: 同步更新项目架构文档中的架构说明、改动史和相关常量，并在 agent 维护约定中保留持续维护要求。
适用范围: dingtalk-agent 项目。

---
memoryId: mem_3647a4f3_32723
timestamp: 2026-10-22T01:32:03Z
scope: project:dingtalk-agent
category: gotcha
title: 长耗时处理期间必须主动展示状态，避免再次静默
tags: [evolve]
confidence: high
trust: self
source.messageIds: proposal:prop_20261022-092337_9bf1
source.agentIds: evolve, user-approved
---

触发条件: 机器人正在思考、调用工具或执行多步骤流程，尚未给出最终回复。
动作: 主动展示当前状态或阶段性进度；遇到工具中断、流程卡顿或等待较久时立即反馈，不能让用户看到长时间静默。
适用范围: dingtalk-agent 的处理中状态展示与任务执行流程。

---
memoryId: mem_3aa1461f_39284
timestamp: 2026-10-22T03:21:24Z
scope: project:dingtalk-agent
category: gotcha
title: autoworkflow.py 视为已知可用实现，接入监控时不得擅改
tags: [evolve]
confidence: high
trust: self
source.messageIds: proposal:prop_20261022-092339_dca9
source.agentIds: evolve, user-approved
---

触发条件: 为 DingTalk Agent 调整监控、后台作业、启动链路或架构。
动作: 只处理该项目自身代码；不要擅自修改 autoworkflow.py，将其视为已知可用的既有实现，除非用户后续明确要求。
适用范围: dingtalk-agent 的代码维护及与 MainToy/autoworkflow.py 的协作边界。

---
memoryId: mem_d5523115_23208
timestamp: 2026-10-23T02:40:08Z
scope: project:dingtalk-agent
category: decision
title: Evolve 作业应避开凌晨并在开机后补跑
tags: [evolve]
confidence: high
trust: self
source.messageIds: card:oc-6d92034967cd400dbd40, session:ses_ea068f460ffel74aIKhwciWQBC, proposal:prop_20261023-092121_37e5
source.agentIds: evolve, user-approved
---

触发条件: 调度 Evolve 作业，且用户机器通常在 08:40 后才开机。
动作: 不再把 Evolve 安排在 03:00；改为用户开机后的上午时段（当前配置 09:20），并设置覆盖当天的补跑窗口，使晚开机时仍能执行且同一 occurrence 只跑一次。
适用范围: dingtalk-agent 的 scheduler/registry.py 中 evolve 作业及其补跑逻辑。

---
memoryId: mem_87af8b34_23211
timestamp: 2026-10-23T02:40:11Z
scope: project:dingtalk-agent
category: gotcha
title: Evolve 多阶段审批必须等待真实用户选择
tags: [evolve]
confidence: high
trust: self
source.messageIds: card:oc-0b2e59dadc9b400da3e2, session:ses_e9eabefcbffeSUtMRjnb2fHOq1, proposal:prop_20261023-092121_31ad
source.agentIds: evolve, user-approved
---

触发条件: DingTalk 卡片回调携带 evolve 的 question_id，尤其是参数 fromConfig 为空的卡片初始化回调。
动作: 将卡片初始化回调与真实用户作答区分；每个审批步骤都必须等待有效用户选择，不得在用户尚未审批时推进下一阶段或显示“未看懂你的选择”，只有收到真实但无效的答复后才提示解析失败。
适用范围: dingtalk-agent 的 Evolve 审批及 DingTalk 卡片回调处理。

