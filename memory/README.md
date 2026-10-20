# memory/ — 记忆层

四类文件，三种信任级别，append-only。

```
memory/
  profile.md                  手写层：用户画像。bot 不得自动改（改要先给 diff + 同意）
  global.md                   长期知识：跨项目通用的 gotcha / pattern / fact
  project_dingtalk-agent.md   本项目专属知识
  journal.md                  append-only 证据流水：每次交互/反馈记一行，只追加
  proposals/                  提案区：evolve 作业唯一可写的地方
    archive/                  已批准的提案（留档）
  rejected/                   被驳回的提案（防止反复提同一个坑）
```

## 信任分级

| level | 来源 | 能进记忆 | 能影响行为 |
|---|---|---|---|
| `self` | 用户明确说的 | ✅ | ✅ |
| `observed` | 工具/数据源看到的 | ✅（带 as_of） | ⚠️ 只作上下文 |
| `external` | 网页/新闻/抓取 | ❌ **永不** | ❌ |

## 写入闸门（`tools/memory_store.py`）

命中即**拒绝整条写入**（不是脱敏后照存）：

- 凭据类：API key（`sk-` / `ark-` / `gh*_` / `AKIA`）、私钥、`password=`/`token=` 赋值、疑似卡号/身份证号
- 敏感属性（健康 / 抑郁 / 宗教 / 政治立场 / 性取向 …）：只有 `trust=self` 才允许
- `trust=external`：无条件拒绝

## 读写接口

```python
from tools import memory_store as ms

ms.append_entry(ms.Entry(scope="global", category="gotcha", title="...",
                          body="触发条件: ...\n动作: ...", confidence="high",
                          trust="self", source_message_ids=["..."]))
ms.index("global")          # 只给 id/标题/类别，agent 需要细节再读全文（省 context）
ms.append_journal("retry", "用户点了重试", conv="...", session="...")
ms.save_proposal({...})      # 提案落 proposals/
ms.list_proposals("pending")
```

## 提案-审批闭环

```
evolve 作业（scheduler 每日 03:00）
  └─ tools/evolve.py —— 直连 LLM（llm_client），**没有工具、没有文件访问**
       └─ 提案写入 memory/proposals/<id>.json
            └─ 推钉钉审批卡
                 ├─ [批准] → tools/apply_proposal.py（确定性代码，不经 LLM）→ 写文件 + git commit
                 └─ [驳回] → 移到 memory/rejected/，下次提议避开
```

**关键隔离**：提案者用 `llm_client` 直连大模型，**物理上没有任何工具**，
改不了记忆库、也改不了自己的评判标准；落地是确定性 Python 代码，不经 LLM。
详见 `docs/self-improvement.md`。
