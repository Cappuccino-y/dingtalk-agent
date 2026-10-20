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
  acceptance.jsonl            落地验收快照（每次批准前后各取一次指标）
  proposals/                  提案区（提案者唯一可写）
    archive/                  已批准
  rejected/                   已驳回（防止反复提同一个坑）

tools/
  memory_store.py             记忆存储层：写入闸门 + 信任分级 + 有界 + 去重 + 提案暂存
  evolve.py                   提案生成（直连 LLM，无工具）
  apply_proposal.py           落地/驳回/废弃（确定性代码，git commit）
  eval_memory.py              健康度指标（便宜，看趋势）
  eval_acceptance.py          变更验收（A/B 真跑，确定性打分）
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

## 五、有界与去重（防记忆退化）

反馈回路最典型的失败不是"提了个坏提案"，而是**记忆单调增长**：同类条目越积越多、
互相矛盾、检索时全是噪音。四条护栏：

| 护栏 | 位置 | 行为 |
|---|---|---|
| **上限** `MAX_LEARNED_RULES=60` | `append_entry` | 每个 scope 的活动条目超限 → **拒绝写入**（不自动淘汰，删东西必须显式） |
| **IMMUTABLE** `tags: [pinned]` | `append_entry` / `retire` | 打了标签的条目，自动落地既不能取代也不能废弃 |
| **证据去重** `evidence_seen` | `evolve` + `approve` | 同一句用户原话已产出过提案（待审/已批/已驳回）→ 跳过 |
| **近重复** `find_similar` | `evolve` + `approve` | 与已存条目相似度 ≥ 0.55 → 推送前就拦掉 |

**为什么用证据去重而不是标题相似度**：标题是 LLM 重写的，同一件事能写成十种措辞，
字符相似度抓不住（实测同义改写只有 0.27）。但 `evidence` 是用户原话，几乎逐字复现。

**删除怎么做**：不物理删除。追加一条 `supersedes: <旧id>` 的 tombstone，
旧条目自动退出活动集（`active_entries` 过滤），`index()` / `brief()` / 检索都不再看到它。
`git revert` 能整条撤销。

## 六、评估

两层，分工明确：

| | `eval_memory.py` | `eval_acceptance.py` |
|---|---|---|
| 回答的问题 | 记忆库健康吗？在长吗？ | **这次改动让回答变好了吗？** |
| 成本 | 零（纯统计） | 每条判例 2 次 LLM 生成 |
| 打分 | 确定性 | 确定性（只有文本是模型生成的） |

### 双切分（double split）

`card_archive.json` 的 30 条真实问答按时间**交替切**：

- **held-out（15 条）** —— 永远不给提案器看，只用于验收
- **held-in（15 条）** —— 提案器可以引用

不切开的话，提案器拿"自己见过的例子"当证据，验收就是自证。回填进 `journal`
的历史问答也按指纹剔除 held-out 部分（否则等于泄漏）。

### 验收怎么做（`--replay`）

1. 对每条 held-out 问题，用确定性相似度找最相关的记忆条目
2. 没有相关条目 → 跳过（没有记忆可用，判不了）
3. 让 LLM 答两遍：**不给记忆** / **给记忆**
4. 确定性打分：答案里出现了多少「相关记忆独有的二元组」

结论 = 有记忆那一遍的命中率平均提升。`Δ ≤ 0` 说明这条记忆没被用上，或是在帮倒忙。

实测一轮（`--replay 5`）：只有 1 条 held-out 命中了相关记忆，
命中率 0.27 → 0.40（Δ=+0.13）。**这说明记忆库太稀疏** —— 大部分问题
还没有对应条目可判，是当前最该补的短板。

**刻意不用 LLM 打分**。按「验证层级」：形式化验证 > LLM judge > 内在自评。
LLM judge 是最容易被 hack 的一档，等确定性指标跑顺了再考虑加。

## 七、刻意没做的事

| 没做 | 为什么 |
|---|---|
| 改 `AGENTS.md` | 安全/规范层，必须手改。提案可以建议，但不自动落地 |
| 改 `opencode.json` | 全局影响面（影响所有项目） |
| 改 `scheduler/registry.py` 的作业定义 | 作业是生产链路 |
| 让 bot 改自己的代码 | 本项目没有测试套件，回归无从验证 |
| 记忆向量化 / 语义检索 | 分层和治理还没跑顺，加检索只会放大问题 |
| 自建 `USER.md` 副本 | 唯一事实源是全局的 `~/.config/opencode/USER.md`，`memory/profile.md` 只是指针 |
| Drift detection 独立检测器 | `git` 的 path-scoped commit 已经是完整审计链，再加一层是重复建设 |
| 三层权限 `/记忆` `/忘记` 命令 | 是产品功能不是安全修复，等记忆条数上去再说 |
| 人格重写（OBOL） | 金融+对话场景，风险收益比差 |

## 八、已知限制

1. **提案质量取决于 LLM**。第一版用「让 LLM 评价项目是否完善」，结果它回答
   「项目已经很完善了」——这是验证层级里最弱的自评。改成「证据里的要求 × 记忆摘要」
   的机械对照后才正常产出。**这是这套东西最脆的一环。**
2. **字符串相似度抓不住中文同义改写**。实测同一件事换种说法只有 0.27，
   所以 `find_similar` 只能拦近重复，真正的同义判断仍靠 LLM 对照记忆摘要。
   诚实说：这是没有 embedding 的必然代价，不是调阈值能解决的。
3. **held-out 只有 15 条，且记忆库稀疏**。`--replay` 经常只判得了 1~2 条
   （大部分问题还没有对应记忆）。样本量小，结论只能看趋势，不能当精确指标。
4. **审批依赖 bot 进程在跑**。bot 挂了卡就点不动（提案本身不会丢，下次扫描会重提）。
5. **journal 只记问题摘要**，不记完整上下文；需要细节时靠 `card_archive.json`。
6. **`card_archive.json` 依赖旧 bot 的行为**（它自己写的），新 bot 不再写这个文件。
   所以判例集是**冻结**的，不会随新交互增长 —— 这是当前最需要补的基础设施。

## 九、相关

- `docs/scheduler.md` —— 调度语义、作业清单
- `docs/bot-architecture.md` —— 整体架构
- `memory/README.md` —— 记忆层速查
- Atelier 的同款协议：`D:\Atelier\server\src\agents\memory.ts`（本项目是它的 Python 移植）
