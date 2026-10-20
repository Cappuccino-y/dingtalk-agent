---
memoryId: mem_b919d356_74772
timestamp: 2026-10-20T05:39:32Z
scope: global
category: gotcha
title: 本机 MCP shell 对 python 子进程管道挂起，验证需用 process launch + 结果落盘通道
tags: [mcp-shell, python, pytest, process-launch, windows, verification, ported-from-atelier]
confidence: high
trust: observed
source.messageIds: msg-skeleton-acceptance, vgE7U6PjT9pZx-VcXvlm_
source.agentIds: lens, atlas, forge
---



---
memoryId: mem_6fd80ea6_74772
timestamp: 2026-10-20T05:39:32Z
scope: global
category: gotcha
title: Windows PowerShell 默认按 GBK 解码无 BOM 的 UTF-8 文件，行数统计与文本比对系统性失真
tags: [powershell, gbk, utf8, encoding, line-count, windows, quantai, ported-from-atelier]
confidence: high
trust: observed
source.messageIds: msg-lens-phase4-resume, msg-atlas-phase4-final, zWi3x4ytAFl0UeDZHTPkk
source.agentIds: lens, atlas
---



---
memoryId: mem_c96073e7_74772
timestamp: 2026-10-20T05:39:32Z
scope: global
category: pattern
title: 无 .git 的工作区项目叠加提交到已有远端历史（update-ref 替换内容法）
tags: [git, update-ref, fast-forward, remote, powershell, quantai, ported-from-atelier]
confidence: high
trust: observed
source.messageIds: msg-atlas-2120-push-done, forge-2118-push-task, atlas-2119-push-result, qjUklQabJGU0K40KXlEBU
source.agentIds: forge, atlas
---



---
memoryId: mem_fe3d8e72_77292
timestamp: 2026-10-20T06:21:32Z
scope: global
category: preference
title: 新增或封装的 Skill 默认配置为全局可用
tags: [convention, approved]
confidence: high
trust: self
source.messageIds: card:oc-e6fd7cd5565d48788842, session:ses_ea8f58a96ffex5UF00fD83GXIj, proposal:prop_20261020-140405_9547
source.agentIds: evolve, user-approved
---

触发条件: 新增、封装或安装 OpenCode Skill。
动作: 将 Skill 注册到全局 Skill 目录，确保所有项目均可使用，不要只配置到当前项目。
适用范围: OpenCode Skill 的创建、封装与安装。

