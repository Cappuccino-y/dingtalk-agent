"""OpenAI 兼容 LLM 客户端 —— 支持**多 key 自动轮换**。

2026-11-12：本机有**两把** MiniMax coding-plan key（`~/.minimax-config.json` 的
`accounts[]`），它们是两个**互相独立的额度池**（实测：两把 key 在
`coding_plan/remains` 上剩余额度不同、到期时间不同）。
以前直连作业（早报 / evolve / 公众号日报）只用一把 —— 额度用完就直接死掉，
整轮作业失败。现在改成轮换：

    一把 key 报「额度不足 / 429」→ 自动换下一把 → 用同一请求继续跑。

设计要点
--------
1. **只对 MiniMax 生效**。火山 Ark 的 `LLM_API_KEY` 不是同一个池子，
   混进去会在 401 上白转一圈 —— 所以按 base_url 判定（`minimaxi` / `minimax`）。
2. **key 池的权威来源是 `~/.minimax-config.json`** —— 跟「查余额」用的是
   同一份文件，改一处两边同步。`MINIMAX_API_KEYS`（逗号分隔）可显式覆盖顺序。
3. **粘性**：某把 key 判定为额度耗尽后，本进程内不再回头用它（避免每批
   都先撞一次墙）。全部耗尽时抛最后一个错误，错误信息里带上「N 把 key 都没了」。
4. **不改变对外形状**：`client.client` 仍是「有 `.chat.completions.create` 和
   `.base_url` 的对象」，`jobs/news_digest.py` 直接用它发请求，不用改。
"""
from __future__ import annotations

import json
import os

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# 「这把 key 用不了了，换下一把」类错误的特征串（中英各来一套）。
#
# 分两类，都该换 key：
#   ① **额度/限流耗尽**：额度用完、频率超限、余额不足。
#   ② **鉴权失败**：key 失效/被撤销/写错了。
#      实测：把 key 末尾改掉几个字符 → MiniMax 返 HTTP 401
#      "login fail: Please carry the API secret key ... (1004)"。
#      这类错误跟「额度」无关，但**同样意味着这把 key 不能用了** ——
#      池子里还有别的 key 时，换一把继续跑比整轮失败划算。
#
# MiniMax 的业务错误码放在响应体 base_resp.status_code 里，官方错误码表：
#   https://platform.minimaxi.com/docs/api-reference/errorcode
#   1002 请求频率超限 / 1004 未授权 / 1008 余额不足 / 2056 超出 Token Plan 资源限制
#   2062 超出 M Plan 速率限制 / 2067 套餐额度已用完
# 码以括号形式出现在 message 里（实测："... (2013)"），所以按 "(1002)" 这种
# 带括号的形式匹配，避免误伤正文里的裸数字。
_QUOTA_PAT = (
    "quota", "insufficient", "rate limit", "ratelimit", "too many requests",
    "exhausted", "arrears", "credit balance", "no credit",
    "余额不足", "额度", "配额", "限流", "已用完", "欠费",
    "(1002)", "(1008)", "(2056)", "(2062)", "(2067)",
    "token plan", "m plan", "m-plan", "资源限制", "速率限制", "套餐额度",
    # 鉴权类（key 失效/写错）：换一把 key 就能继续，不该整轮失败。
    "(1004)", "(2049)", "login fail", "not authorized", "unauthorized",
    "api key or ak/sk", "api key is invalid", "invalid api key",
)

# HTTP 状态码：这些直接判定「这把 key 不能用了」。
# 401/403 = 鉴权失败；402 = 欠费；429 = 限流/额度。
_KEY_FATAL_STATUS = (401, 402, 403, 429)


def _is_quota_error(exc: BaseException) -> bool:
    """这个异常是不是「这把 key 用不了了，换一把」类错误。"""
    status = getattr(exc, "status_code", None)
    if status in _KEY_FATAL_STATUS:
        return True
    msg = str(exc).lower()
    return any(p in msg for p in _QUOTA_PAT)


def _is_minimax(base_url: str | None) -> bool:
    return bool(base_url) and ("minimaxi" in base_url.lower()
                               or "minimax" in base_url.lower())


def _minimax_key_pool() -> list[str]:
    """两把 MiniMax key 的来源，按优先级拼接 + 去重。

    顺序：`MINIMAX_API_KEYS`（逗号分隔，显式指定优先级）
       → `MINIMAX_API_KEY`（老变量，保持向后兼容）
       → `~/.minimax-config.json` 的 `accounts[]` / `token`
          （**权威来源**：跟「查余额」用同一份文件，改一处两边同步）
    """
    pool: list[str] = []

    def add(key: str | None) -> None:
        key = (key or "").strip()
        if key and key not in pool:
            pool.append(key)

    for key in (os.getenv("MINIMAX_API_KEYS") or "").split(","):
        add(key)
    add(os.getenv("MINIMAX_API_KEY"))
    try:
        cfg_path = os.path.expanduser("~/.minimax-config.json")
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        if isinstance(cfg, dict):
            accounts = cfg.get("accounts")
            if isinstance(accounts, list) and accounts:
                for acct in accounts:
                    if isinstance(acct, dict):
                        add(acct.get("token"))
            else:
                add(cfg.get("token"))
    except (OSError, ValueError):
        pass  # 文件不存在/坏了 → 只用 env 里的 key，不影响主流程
    return pool


class _RotatingCompletions:
    """`client.client.chat.completions.create` 的替身：带 key 轮换。"""

    def __init__(self, owner: "RotatingOpenAI") -> None:
        self._owner = owner

    def create(self, **kwargs):
        return self._owner.create(**kwargs)


class _RotatingChat:
    def __init__(self, owner: "RotatingOpenAI") -> None:
        self.completions = _RotatingCompletions(owner)


class RotatingOpenAI:
    """对外形状和 `openai.OpenAI` 一致，内部按 key 轮换。

    只在**确定是额度问题**时才换 key —— 网络抖动、参数错误这类跟 key 无关的
    故障，换一把只会把同一个错误再撞一遍，还白白消耗另一把 key 的额度。
    """

    def __init__(self, keys: list[str], base_url: str | None = None,
                 timeout: float | None = None) -> None:
        self._keys = [k for k in keys if k]
        self._clients = [OpenAI(api_key=k, base_url=base_url, timeout=timeout)
                         for k in self._keys]
        self._dead: set[int] = set()
        self._idx = 0
        self.base_url = base_url
        self.chat = _RotatingChat(self)

    @property
    def api_key(self) -> str:
        """当前生效的 key（部分代码会读它做日志/诊断）。"""
        return self._keys[self._idx] if self._keys else ""

    def _live_indexes(self) -> list[int]:
        if len(self._dead) >= len(self._keys):
            # 全挂了 → 复活一遍再试（额度窗口可能刚重置）
            self._dead.clear()
        return [i for i in range(len(self._keys)) if i not in self._dead]

    @staticmethod
    def _check_business_error(resp) -> None:
        """MiniMax 把业务错误码放在响应体 `base_resp.status_code` 里。

        官方文档明确说了「多数情况下业务错误仍返回 HTTP 200」—— 也就是说
        **openai SDK 不会抛异常**，而是返回一个 choices 为空的「成功」响应。
        不在这里拦一下，额度耗尽时就会静默拿到空回复（用户看到「（空回复）」），
        永远不会触发换 key。

        所以：只要 base_resp 报的是额度类错误码，就抛一个带原始文案的异常，
        交给上层判定要不要换 key。
        """
        base = getattr(resp, "base_resp", None)
        if base is None and isinstance(resp, dict):
            base = resp.get("base_resp")
        if not base:
            return
        code = getattr(base, "status_code", None)
        if code is None and isinstance(base, dict):
            code = base.get("status_code")
        msg = getattr(base, "status_msg", None)
        if msg is None and isinstance(base, dict):
            msg = base.get("status_msg")
        if not code:  # 0 = 成功
            return
        detail = f"MiniMax base_resp 错误码 {code}: {msg or ''}".strip()
        if _is_quota_error(RuntimeError(detail)):
            raise RuntimeError(detail)
        # 非额度类业务错误：SDK 没抛，但这次调用其实失败了 → 显式抛出去，
        # 别把空 choices 当成正常回复交上去。
        raise RuntimeError(detail)

    def create(self, **kwargs):
        last: BaseException | None = None
        for idx in self._live_indexes():
            self._idx = idx
            try:
                resp = self._clients[idx].chat.completions.create(**kwargs)
                self._check_business_error(resp)
                return resp
            except Exception as exc:  # noqa: BLE001
                last = exc
                if not _is_quota_error(exc):
                    raise
                self._dead.add(idx)
                print(f"[llm] key #{idx + 1}/{len(self._keys)} 额度不可用："
                      f"{str(exc)[:120]} → 换下一把")
        assert last is not None
        raise last


class OpenAICompatibleClient:
    """兼容 OpenAI 接口的 LLM 客户端，支持单轮与多轮对话。"""

    def __init__(self, model: str = None, api_key: str = None,
                 base_url: str = None, api_keys: list[str] | None = None):
        self.model = model or os.getenv("LLM_MODEL_ID")
        base_url = base_url or os.getenv("LLM_BASE_URL")
        self.base_url = base_url

        if api_keys:
            # 调用方**显式指定**了 key 池 → 就用这一组，不再合并全局池
            # （显式指定 = 调用方清楚自己要哪些 key）。
            keys = [k for k in api_keys if k]
        else:
            keys = []
            if api_key:
                keys = [api_key]
            if _is_minimax(base_url):
                # MiniMax：把整个 key 池接上（显式给的 key 排最前）
                for key in _minimax_key_pool():
                    if key not in keys:
                        keys.append(key)
        if not keys:
            keys = [os.getenv("LLM_API_KEY") or ""]

        self.api_keys = keys
        self.client = (RotatingOpenAI(keys, base_url) if len(keys) > 1
                       else OpenAI(api_key=keys[0] if keys else None,
                                   base_url=base_url))

    @classmethod
    def for_job(cls, prefix: str = "EVOLVE"):
        """按前缀读环境变量建客户端，缺配置就退回默认的 LLM_*。

        EVOLVE_LLM_BASE_URL / EVOLVE_LLM_MODEL / EVOLVE_LLM_API_KEY
        （API key 不给就用 MINIMAX_API_KEY，再不给就用 LLM_API_KEY）
        """
        model = os.getenv(f"{prefix}_LLM_MODEL")
        base = os.getenv(f"{prefix}_LLM_BASE_URL")
        key = os.getenv(f"{prefix}_LLM_API_KEY") or os.getenv("MINIMAX_API_KEY")
        if not (model and base):
            return cls()
        return cls(model=model, api_key=key, base_url=base)

    def generate(self, prompt: str, system_prompt: str = None) -> str:
        """单轮对话：传入 prompt 和可选的 system_prompt，返回模型回复。"""
        print("正在调用大语言模型（单轮）...")
        try:
            messages = []
            if system_prompt:
                messages.append({'role': 'system', 'content': system_prompt})
            messages.append({'role': 'user', 'content': prompt})

            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=False
            )
            answer = response.choices[0].message.content
            print("大语言模型响应成功。")
            return answer
        except Exception as e:
            print(f"调用LLM API时发生错误: {e}")
            raise  # 抛出异常，让上层统一处理

    def chat(self, messages: list) -> str:
        """
        多轮对话：传入完整的消息列表，返回模型回复。
        """
        print("正在调用大语言模型（多轮对话）...")
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=False
            )
            answer = response.choices[0].message.content
            print("大语言模型响应成功。")
            return answer
        except Exception as e:
            print(f"调用LLM API时发生错误: {e}")
            raise


if __name__ == "__main__":
    client = OpenAICompatibleClient()
    print(client.generate("你好"))
