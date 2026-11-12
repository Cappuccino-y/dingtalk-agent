"""兜底模型候选链 —— 主模型不可用时按顺序换模型重试。

2026-11-12 真机事故（根因之一）：
    火山 Agent Plan 订阅到期 → 主模型秒失败。桥接层本来有「自动切兜底模型
    再重试」的机制，但当时的守卫写的是

        cur_pid != FALLBACK_MODEL["providerID"]      # ← 只比 providerID

    默认模型改成 minimax-cn-coding-plan/MiniMax-M3.1-Flash 之后，兜底模型
    也在 minimax-cn-coding-plan 下 → 这条守卫**恒为 False** → 兜底分支
    根本进不去 → 用户只收到「（空回复）」。

    更深一层：**同一个 provider 往往共用同一份订阅/配额池**。主模型报
    「额度耗尽」时，同 provider 的兄弟模型大概率一起挂 —— 所以兜底必须
    **跨 provider**，同 provider 的只能排到最后当最后一张牌。

本模块只放纯逻辑（不碰网络、不依赖钉钉 SDK），方便单测直接钉住规则：

    python tests/test_model_fallback.py
"""
from __future__ import annotations

# 兜底候选链（按优先级）。
# ⚠ model id 必须真实存在于本机 /api/model 清单里：切换接口**不校验 id**，
#   写了不存在的 id 也返回 204「切成功」，但下一轮压根不产生 assistant
#   消息 → 用户只拿到一张空卡片 + 一句假提示（2026-10-22 踩过）。
# ⚠ providerID 也必须真实存在（各 provider 的 key 由本机 opencode 配置提供）。
#
# ⚠ 2026-11-12：MiniMax 有**两把 key**（两把都是 coding plan，各自独立额度池）。
#   「providerID 不同」不等于「额度池不同」，也不等于「模型不同」——
#   `minimax2` 这一个 provider 下挂着两把 key 下的多个模型，
#   它们跟 `minimax-cn-coding-plan` 共享与否要**按 key 算**。
#   所以这里引入显式的 **pool**（额度池）概念：
#     pool 相同的模型 = 同一份订阅额度，一起挂 → 排序要分开；
#     pool 不同的模型 = 独立额度，才能当真正的兜底。
#   两把 MiniMax key 因此和 deepseek 一样，是**平级的独立兜底**。
FALLBACK_CHAIN: tuple[dict, ...] = (
    # 第二把 MiniMax key（Key 2）上的**同一个模型**：
    # 模型不降级、只换 key —— 首选兜底。
    {"providerID": "minimax2", "id": "MiniMax-M3.1-Flash", "pool": "minimax-key2"},
    # DeepSeek 官方 key：与 MiniMax 完全独立的账号/额度。
    {"providerID": "deepseek", "id": "deepseek-v4-pro", "pool": "deepseek"},
    # 免费档：前两个都挂了时的最后一张牌（质量一般，但能出字）。
    {"providerID": "opencode", "id": "minimax-m3.1-flash-free", "pool": "opencode-free"},
    # 第二把 key 的老模型：跟链首**共用 Key 2 额度池**，Key 2 挂了它多半一起挂，
    # 排到 deepseek 之后。
    {"providerID": "minimax2", "id": "MiniMax-M3", "pool": "minimax-key2"},
    # 默认模型同 provider 的兄弟模型：跟默认模型**共用 Key 1 额度池**，
    # 主模型额度耗尽时它多半也挂 —— 但「模型下线/鉴权挂了」这类故障它能救。
    {"providerID": "minimax-cn-coding-plan", "id": "MiniMax-M3", "pool": "minimax-key1"},
)

# providerID → 额度池。**按 key 算，不按 provider 算**：
# 同一家 MiniMax 的两个 provider 挂着两把不同的 key → 两个独立的额度池。
# 认不出来的 provider 一律自己成一个池（保守：不会误判成共享额度）。
POOL_BY_PROVIDER: dict[str, str] = {
    "minimax-cn-coding-plan": "minimax-key1",
    "minimax2": "minimax-key2",
    "deepseek": "deepseek",
    "opencode": "opencode-free",
}


def pool_of(provider_id: str, entry: dict | None = None) -> str:
    """某个候选模型属于哪个额度池。

    链里显式写了 `pool` 就用它；否则按 providerID 查表；再查不到就
    「一个 provider 一个池」——宁可当成独立的（多试一次），
    也不要误判成共享额度（那样会把唯一的活牌排到最后）。
    """
    if entry and entry.get("pool"):
        return entry["pool"]
    return POOL_BY_PROVIDER.get(provider_id, provider_id)


# 兼容旧引用（jobs/premarket_push.py 等按 FALLBACK_MODEL["id"] 用）。
FALLBACK_MODEL: dict = dict(FALLBACK_CHAIN[0])


def candidates(available, failed=None, tried=None) -> list[dict]:
    """从兜底链里挑出**本机真的存在**、且还没试过的候选。

    参数
    ----
    available : list[dict]
        本机 /api/model 清单，形如 [{"providerID": ..., "id": ...}, ...]。
        **空列表一律返回空**：取不到清单 = 无法验证 id 存在，宁可不切，
        也不能切一个不存在的 id（会拿到「切成功 + 空卡片」）。
    failed : tuple | None
        刚失败的那个模型 (providerID, id)。它自己不能再当兜底。
    tried : set | None
        已经试过的 (providerID, id)，用于一轮里连续换多个候选。

    排序规则
    --------
    **跨额度池**的候选排前面，同池的排后面 —— 见模块头说明。
    判据是 pool（哪把 key），**不是 providerID**：MiniMax 的两个 provider
    挂着两把不同的 key，是两个独立额度池，必须当平级的独立兜底。
    同组内保持 FALLBACK_CHAIN 声明的优先级（稳定排序）。
    """
    if not available:
        return []
    have = {(m.get("providerID"), m.get("id")) for m in available}
    done = set(tried or ())
    if failed:
        done.add(tuple(failed))

    picked: list[dict] = []
    for c in FALLBACK_CHAIN:
        pair = (c.get("providerID"), c.get("id"))
        if pair in done or pair not in have:
            continue
        picked.append(dict(c))

    failed_provider = (failed or ("", ""))[0]
    failed_pool = pool_of(failed_provider)
    cross = [c for c in picked
             if pool_of(c.get("providerID"), c) != failed_pool]
    same = [c for c in picked
            if pool_of(c.get("providerID"), c) == failed_pool]
    return cross + same
