# 自我改进闭环（self-improvement）

> 让机器人从对话里学到东西，然后**改自己的记忆/约定**——但改之前必须过你这一关。

## 一、为什么要这么设计

「让 bot 自己改自己」是整个 agent 领域里最容易出事的方向。业界踩过的坑
（[autoevolve 的 SKILL.md](https://github.com/guanyang/open-agent-hub/blob/main/skills/self-improvement-loops/SKILL.md)
有完整复盘）：

- agent **删掉了** hallucination detector 依赖的 log marker
- agent **覆盖了** timing function、monkey-patch 评估器让它返回满分
- 而且**当 agent 能看见打分函数时，hack 变多了**
- 自改指令的 agent 会学到「讨好用户」而不是「解决问题」
- 变更无声累积后，没人能说清什么时候、为什么、基于什么证据变的

所以本设计的三条铁律：

| 铁律 | 本项目的实现 |
|---|---|
| **评估器 / 权限 / 预算住在 loop 改不到的地方** | 提案者**直连 LLM，没有任何工具** → 物理上改不了任何文件 |
| **提案权 ≠ 变更权** | 提案落 `memory/proposals/`，你点「批准」才落地 |
| **落地是确定性代码，不经 LLM** | `tools/apply_proposal.py` 只做白名单校验 + 追加 + git commit |

再补一条来自研究的关键结论（[arXiv 2605.08442](https://arxiv.org/html/2605.08442)）：
**注入成功 ≠ 执行成功**。所以「防住写入」不够，执行侧必须结构性隔离——
这就是为什么提案者干脆不给工具。

## 二、目录

```
memory/
  profile.md                  手写层：用户画像。bot 不得自动改（改要先给 diff + 同意）
  global.md                   长期知识（跨项目通用）
  project_dingtalk-agent.md   本项目专属知识
  journal.md                  append-only 证据流水（每次交互/反馈一行）
  proposals/                  提案区（提案者唯一可写）
    archive/                  已批准
  rejected/                   已驳回（防止反复提同一个坑）

tools/
  memory_store.py             记忆存储层：写入闸门 + 信任分级 + 提案暂存
  evolve.py                   提案生成（直连 LLM，无工具）
  apply_proposal.py           落地/驳回（确定性代码，git commit）
  eval_memory.py              评估器（确定性指标，住在 loop 外）
```

## 三、跑起来是什么样

```
[每日 03:00]  scheduler 触发 evolve 作业（跑完就退的子进程）
   │
   ├─ 读证据：journal + 记忆索引 + 被驳回的提案 + 问答存档
   │
   ├─ LLM（直连 MiniMax，**无工具**）做「机械对照」：
   │     把证据里用户明确提出的要求/纠正挑出来
   │     × 和记忆索引对照，找出**没被覆盖**的
   │     → 最多 3 条提案
   │
   ├─ 提案落 memory/proposals/<id>.json
   │
   └─ 推钉钉审批卡（ASK 卡片，question_id 里编了提案 id）

[你在钉钉点「批准」]
   │
   ├─ bot 进程收到回调 → 按提案 id 读 JSON（无状态，bot 重启也不影响）
   ├─ apply_proposal.py：校验白名单 → 写入门禁 → 追加记忆 → git commit
   └─ 回写卡片状态（已批准 / 已驳回 / 已暂缓）
```

手动触发：

```
python -m scheduler run evolve        # 手动补跑一次
python tools/evolve.py --dry-run      # 只看提案，不落盘不推卡
python tools/evolve.py --backfill-cards   # 把历史问答回填进 journal
python tools/apply_proposal.py --list # 看待审提案
python tools/apply_proposal.py <id> --approve
python tools/apply_proposal.py <id> --reject --note "理由"
python tools/eval_memory.py --save    # 指标快照
```

## 四、记忆的写入闸门

命中即**拒绝整条写入**（不是脱敏后照存）——见 `tools/memory_store.py`：

| 类别 | 规则 |
|---|---|
| 凭据 | API key（`sk-` / `ark-` / `gh*_` / `AKIA`）、私钥、`password=`/`token=` 赋值、疑似卡号/身份证号 |
| 敏感属性 | 健康 / 抑郁 / 宗教 / 政治立场 / 性取向 … **只有 `trust=self`（用户明说）才允许** |
| 外部内容 | `trust=external`（网页/新闻/抓取）**无条件拒绝** |

**信任分级**（对应 ApertoMemory 的 trust level / TMA-NM 的 origin binding）：

| level | 来源 | 进记忆 | 影响行为 |
|---|---|---|---|
| `self` | 用户明确说的 | ✅ | ✅ |
| `observed` | 工具/数据源看到的 | ✅（带 as_of） | ⚠️ 只作上下文 |
| `external` | 网页/新闻/抓取 | ❌ | ❌ |

## 五、评估

`tools/eval_memory.py` 只做**可复现的统计**，不做主观判断：

- 问答量、回答长度分布、问题关键词命中率
- 用户反馈信号（retry / stop / like）——最硬的负反馈
- 记忆库健康度（条数/分类/置信度/信任分布）
- 提案闭环（提出/批准/驳回/批准率）

**刻意不用 LLM 打分**。按「验证层级」：形式化验证 > LLM judge > 内在自评。
LLM judge 是最容易被 hack 的一档，等确定性指标跑顺了再考虑加。

## 六、刻意没做的事

| 没做 | 为什么 |
|---|---|
| 改 `AGENTS.md` | 安全/规范层，必须手改。提案可以建议，但不自动落地 |
| 改 `opencode.json` | 全局影响面（影响所有项目） |
| 改 `scheduler/registry.py` 的作业定义 | 作业是生产链路 |
| 让 bot 改自己的代码 | 本项目没有测试套件，回归无从验证 |
| 记忆向量化 / 语义检索 | 分层和治理还没跑顺，加检索只会放大问题 |
| 自建 `USER.md` 副本 | 唯一事实源是全局的 `~/.config/opencode/USER.md`，`memory/profile.md` 只是指针 |

## 七、已知限制

1. **提案质量取决于 LLM**。第一版用「让 LLM 评价项目是否完善」，结果它回答
   「项目已经很完善了」——这是验证层级里最弱的自评。改成「证据里的要求 × 记忆索引」
   的机械对照后才正常产出。**这是这套东西最脆的一环。**
2. **审批依赖 bot 进程在跑**。bot 挂了卡就点不动（提案本身不会丢，下次扫描会重提）。
3. **没有跨会话的语义去重**。同一个坑提两次会生成两条记忆，靠「驳回记录」和
   记忆索引对照人工压制。
4. **journal 只记问题摘要**，不记完整上下文；需要细节时靠 `card_archive.json`。
5. **`card_archive.json` 依赖旧 bot 的行为**（它自己写的），新 bot 不再写这个文件。

## 八、相关

- `docs/scheduler.md` —— 调度语义、作业清单
- `docs/bot-architecture.md` —— 整体架构
- `memory/README.md` —— 记忆层速查
- Atelier 的同款协议：`D:\Atelier\server\src\agents\memory.ts`（本项目是它的 Python 移植）
