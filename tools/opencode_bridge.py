"""OpenCode 桥接模块：把钉钉消息交给本机 OpenCode agent，并把结果渲染成钉钉 AI 卡片。

设计要点（均为实测结论）：
- 服务发现：OpenCode 后台服务的端口/密码是随机的，运行时从
  ~/.local/state/opencode/service.json 读取。
- 三段式调用：POST /api/session/{id}/prompt 投递 → POST
  /api/experimental/session/{id}/wait 等空闲 → GET /api/session/{id}/message 取回答。
- 附件 uri 必须是 file:/// 形式，给 Windows 原生路径会被拒。
- 会话级持久指令用 PUT /api/experimental/session/{id}/instructions/entries/{key}，
  用来告诉 agent「你在手机钉钉里说话」。
- 钉钉卡片有**两套**渲染面，markdown 支持程度不一样，别混为一谈：
  * v1（SDK 自带公共模板 `382e4302…`，只有 msgContent + staticMsgContent）：
    加粗/斜体/引用/链接/图片/换行有效；标题(#)/列表/分隔线/代码块/表格**无效**
    —— 由 normalize_markdown 兜底转换。
  * v2（公共模板 `675cde2f…` + 卡片 OpenAPI，见 tools/card_v2.py）：支持完整
    markdown（标题/列表/表格/代码块/分隔线/引用），另有 blockList 结构化分层、
    单行进度块、底部状态行、引用原文、卡片内图片、停止/重试按钮、结构化提问卡。
  * v2 默认**关闭**（`.env` 里 `DINGTALK_CARD_V2=0`）；置 1 才启用，建卡失败
    会自动回退到 v1，不会让机器人不可用。
  * 2026-10-19 13:40：**v2 已重新打开**（真机自检卡渲染验证通过）。过程中修掉 6 个 bug：
    ① 遗留 v2 卡片曾用 v1 接口收尾（静默失败 -> 永远停在「输入中」）；
    ② 自造 `type=4` 按钮块——该模板 blockList 只认 0-3，按钮/重试/提问全是死代码；
    ③ 局部更新漏 `cardUpdateOptions.updateCardDataByKey`（改状态行会清掉正文 = 空白）；
    ④ 卡片回调真实字段是 `cardPrivateData.actionIds`，旧解析只认 `actionId`；
    ⑤ v2 链路原本没有 QPS 限流与退避重试（v1 有）；
    ⑥ 提问卡关着时仍会干等 180s 才回退。
    结构化提问卡是另一张表单模板（`DINGTALK_ASK_CARD`），默认关，等真机点选验证。
"""
import base64
import difflib
import json
import logging
import os
import queue
import re
import threading
import time
import uuid

import requests
from dingtalk_stream.card_instance import AIMarkdownCardInstance
from dingtalk_stream.card_replier import AICardStatus

# v2 卡片链路（公共模板 + 卡片 OpenAPI）。导入失败**不能**影响机器人启动——
# 顶多是回退到 SDK 卡片，所以这里吞掉异常。
try:
    from tools import card_v2 as card_v2
except Exception:  # noqa: BLE001
    try:
        import card_v2 as card_v2
    except Exception:  # noqa: BLE001
        card_v2 = None

logger = logging.getLogger("opencode_bridge")

SERVICE_JSON = os.path.expanduser(r"~\.local\state\opencode\service.json")

DEFAULT_MODEL = {"providerID": "volcengine-agent-plan", "id": "deepseek-v4.1-flash"}
# 兜底模型：火山 Agent Plan 额度耗尽 / 报错时自动切到 MiniMax 继续回复。
# providerID / model id 已对着本机 /api/model 接口核实过。
FALLBACK_MODEL = {"providerID": "minimax-cn-coding-plan",
                  "id": "MiniMax-M3.1-Flash-Preview"}

# /model 提问卡里给出的可选模型（手机端点选，避免手打 id 打错）。
# key = "providerID|modelID"：同一个模型 id 可能挂在多个 provider 下
# （例如 minimax-m3），只写 id 会有歧义。
MODEL_CHOICES = [
    {"key": "minimax-cn-coding-plan|MiniMax-M3.1-Flash",
     "label": "MiniMax 3.1", "id": "MiniMax-M3.1-Flash"},
    {"key": "volcengine-agent-plan|deepseek-v4.1-flash",
     "label": "DeepSeek 4.1 Flash", "id": "deepseek-v4.1-flash"},
    {"key": "volcengine-agent-plan|glm-5.3-flash",
     "label": "GLM 5.3 Flash", "id": "glm-5.3-flash"},
]
# 提问卡等用户点选的最长时间（秒）；超时就把卡片收成「已失效」，不干等。
MODEL_CHOICE_TIMEOUT = 120

# 命中这些关键词即认为"主模型不可用"，触发切模型。
# 注意别用 "exceed" 这种宽泛词——它会把 "context length exceeded"（上下文超长，
# 切模型没用）也误判成额度问题；火山真实的报错是 "...exceeded the 5-hour usage quota"，
# 靠 "quota" 就能命中。
_QUOTA_ERROR_PAT = (
    "quota", "insufficient", "rate limit", "ratelimit", "too many requests",
    "exhausted", "balance", "billing", "arrears", "欠费", "额度", "配额", "限流",
    "余额不足", "out of credit", "no credit", "overdue",
)

POLL_INTERVAL = 0.6
MAX_PROGRESS_LINES = 6
MAX_ANSWER_CHARS = 2800
WAIT_TIMEOUT = 900
# 预判式轮换：**默认关闭**。
# 原因：OpenCode 自带 compaction（auto 默认开，keep 15000，buffer 10%），
# 接近上限时会自动把旧消息压成摘要并保留最近 15k tokens；
# 而手工轮换只能靠一段交接摘要续命，**丢得比内置压缩更多**，
# 所以默认不启用。实测上下文窗口 1M，当前会话仅用 53k（5%），远未到压力。
# 需要时把它设成 >0 即可开启（例如想主动缩短单会话长度）。
ROTATE_TURNS = 0
# 轮换时生成交接摘要的超时（秒）；超时就直接开新会话，不硬等。
HANDOVER_TIMEOUT = 45

# ========== 实时性 / 卡片限流 / 并发 相关参数（均可用环境变量覆盖）==========
def _env_float(name, default):
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _env_int(name, default):
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _env_flag(name, default=False):
    v = str(os.getenv(name, "") or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


MODEL_CHOICE_TIMEOUT = _env_float("DINGTALK_MODEL_CHOICE_TIMEOUT", MODEL_CHOICE_TIMEOUT)


# 事件流（SSE /api/event）重连退避（秒）。
EVENT_RECONNECT_DELAYS = (0.5, 1, 2, 5, 10)
# 兜底轮询间隔（秒）与「事件静默多久算落后」的阈值。
# 事件流是**易失**的（官方：慢消费者会断流、断线期间事件会丢），所以必须有兜底。
POLL_FALLBACK_INTERVAL = _env_float("OC_POLL_FALLBACK", 3.0)
EVENT_STALE_AFTER = _env_float("OC_EVENT_STALE", 6.0)
# 权限请求 / question 表单的兜底检查间隔（秒）。手机钉钉没有应答 UI，
# 挂起项必须我们主动应答，否则整轮会干等到超时。
GATE_CHECK_INTERVAL = _env_float("OC_GATE_CHECK", 2.0)

# 卡片防抖：钉钉 AI 卡片 streaming 接口要求 markdown 全量更新（isFull=true），
# 单次内容建议 ≤1KB、总量 ≤10KB；官方连接器实测 800ms 防抖 + 全局 20 QPS
# 令牌桶 + 撞 403/QpsLimit 退避重试最稳。
CARD_MIN_INTERVAL = _env_float("DINGTALK_CARD_MIN_INTERVAL", 0.4)
CARD_MAX_BYTES = _env_int("DINGTALK_CARD_MAX_BYTES", 10 * 1024)
CARD_QPS = _env_int("DINGTALK_CARD_QPS", 20)
CARD_RETRY_DELAY = _env_float("DINGTALK_CARD_RETRY_DELAY", 2.0)

# 自动放行的「低风险」权限动作；其余一律自动拒绝（并提示用户），
# 避免手机端无法应答导致整轮卡死。
AUTO_ALLOW_PERMISSIONS = {"read", "glob", "grep", "list", "webfetch", "websearch",
                          "skill", "todo"}
# question 表单无法在手机钉钉回答，回这句话让 agent 改用纯文本提问。
FORM_UNSUPPORTED_ANSWER = "（手机钉钉不支持交互式问答，请改用纯文本提问）"

# ========== 会话指令（精简版）==========
# 完整写作规范 / 通用能力 / 文件推送 / 行情交易框架**统一放在 Location 的
# AGENTS.md**（.opencode-bot/AGENTS.md），避免同一套规则在三个地方各写一遍、
# 改一处忘一处互相漂移。这里只留最关键、最容易出错的几条做冗余保险。
BRIEF_KEY = "dingtalk.mobile-brief"
BRIEF_TEXT = """你在**手机钉钉**的卡片里回复用户。硬性规则（与工作目录 AGENTS.md 一致，
冲突时以 AGENTS.md 为准）：
1. 先结论后细节，默认 ≤15 行；不用表格、不用 `#` 标题、不用 ``` 代码围栏。
2. 需要用户确认/选择时**优先用正文文字问**；要把多个选项摆出来时可以用
   question 工具，但**每个问题必须带选项**（最多 6 个，用户在卡片上点按钮作答），
   不要设计需要自由文本输入的字段（手机端点不了，会退回文字提问）。
3. 行情 / 点位 / 持仓类问题走 AGENTS.md 里的交易分析框架，
   且**必须先看隔夜外围**（market_overseas）再下结论。
4. 长内容先给 3-5 行摘要，再问是否展开；不要客套话。
"""

INSTRUCTIONS = {BRIEF_KEY: BRIEF_TEXT}


def apply_instructions(oc, sid):
    """幂等地把「精简会话提示」挂上。

    完整规范（写作 / 通用能力 / 文件推送 / 交易框架）统一在 Location 的
    AGENTS.md 里，这里只做冗余保险，见 BRIEF_TEXT 的注释。
    """
    ok = True
    for key, value in INSTRUCTIONS.items():
        try:
            oc.set_instruction(sid, key, value)
        except Exception as e:  # noqa: BLE001
            logger.warning("instruction %s failed for %s: %s", key, sid, e)
            ok = False
    return ok


# ========== 运行时状态：在飞回合登记表 + 重启请求 ==========
# 为什么需要持久化：进程被重启 / 强杀时，**正在渲染的那张 AI 卡片会停在
# loading**（钉钉显示「正在思考…[AILoading]」+「🔧 正在执行」），因为收尾
# （ai_finish）没来得及发。新进程启动时读这张登记表，把孤儿卡收尾即可。
_HERE_DIR = os.path.dirname(os.path.abspath(__file__))
_MAINTOY_DIR = os.path.dirname(_HERE_DIR)
RUNTIME_DIR = (os.getenv("BRIDGE_DATA_DIR")
               or os.path.join(_MAINTOY_DIR, "bot", "bridge_data"))
INFLIGHT_FILE = os.path.join(RUNTIME_DIR, "inflight.json")
RESTART_FLAG = os.path.join(RUNTIME_DIR, "restart.request")


def _atomic_write_json(path, payload):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


class InflightRegistry:
    """「还没把最终回答发出去」的回合登记表（跨进程可读）。

    结构：{conv_id: {conv_id, session_id, card_id, title, user_text,
                    started_at, pid, status}}
    回合开跑时 begin()、回答投递完 end()。重启后新进程读 snapshot()
    把遗留卡片收尾，用户不会看到一张永远转圈的僵尸卡。
    """

    _lock = threading.Lock()

    def __init__(self, path=INFLIGHT_FILE):
        self.path = path

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict):
        try:
            _atomic_write_json(self.path, data)
        except OSError as e:  # noqa: BLE001
            logger.warning("写在飞回合登记表失败：%s", e)

    def begin(self, conv, session_id, card_id=None, title="OpenCode",
              user_text="", card_v2=False):
        with self._lock:
            data = self._load()
            data[str(conv)] = {
                "conv_id": str(conv),
                "session_id": session_id or "",
                "card_id": card_id or "",
                "card_v2": bool(card_v2),
                "title": title or "OpenCode",
                "user_text": (user_text or "")[:200],
                "started_at": time.time(),
                "pid": os.getpid(),
                "status": "running",
            }
            self._save(data)

    def set_status(self, conv, status):
        with self._lock:
            data = self._load()
            rec = data.get(str(conv))
            if rec:
                rec["status"] = status
                self._save(data)

    def end(self, conv):
        with self._lock:
            data = self._load()
            if data.pop(str(conv), None) is not None:
                self._save(data)

    def snapshot(self) -> list:
        with self._lock:
            return list(self._load().values())

    def clear(self):
        with self._lock:
            self._save({})


INFLIGHT = InflightRegistry()


# ---- 重启请求（协同退出，绝不在自己这一轮里 kill 自己）----
def request_restart(reason="", by=""):
    """只写「重启请求」标记；真正的退出由监听进程在空闲时执行。

    为什么要这样：在自己这一轮里 Stop-Process 会连**正在渲染这张卡片的
    进程**一起杀掉，卡片就永远停在「正在思考…」。写标记则让进程在
    「这一轮跑完 + 卡片收尾」之后再退出。
    """
    _atomic_write_json(RESTART_FLAG, {
        "requested_at": time.time(),
        "requested_at_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "reason": reason or "",
        "by": by or "",
        "pid": os.getpid(),
    })
    return RESTART_FLAG


def restart_request() -> dict:
    try:
        with open(RESTART_FLAG, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def restart_pending() -> bool:
    return bool(restart_request())


def clear_restart():
    try:
        os.remove(RESTART_FLAG)
    except OSError:
        pass


class _CardMsgStub:
    """AIMarkdownCardInstance 需要的最小 incoming_message 占位对象。

    收尾孤儿卡只用到 card_instance_id；但 get_card_data() 会读
    incoming_message.hosting_context，所以必须给个有该属性的桩。
    """
    hosting_context = None


def finalize_orphan_card(client, card_id, body, footer="", title="OpenCode",
                         failed=False) -> bool:
    """用 **v1（SDK 卡片）接口**把遗留的 loading 卡片强制收尾。

    client = DingTalkStreamClient（仅用于取 access_token）。
    返回 True 表示**已发起**收尾请求：钉钉 SDK 内部会吞掉 HTTP 错误
    （card 不存在 / 限流都只写日志），所以这里无法区分接口是否真的成功。
    """
    if not card_id:
        return False
    raw = (body or "").encode("utf-8")
    if len(raw) > CARD_MAX_BYTES:
        body = (raw[:CARD_MAX_BYTES - 24].decode("utf-8", "ignore")
                + "\n\n…（已截断）")
    try:
        card = AIMarkdownCardInstance(client, _CardMsgStub())
        card.card_instance_id = card_id
        card.title = title or "OpenCode"
        card.markdown = (body or "").strip() or "（重启前这一轮没有留下回答）"
        card.static_markdown = footer or ""
        card.streaming(card_id, "msgContent", card.markdown, append=False,
                       finished=True, failed=failed)
        card.finish(card_id, card.get_card_data())
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("收尾孤儿卡失败（%s）：%s", card_id, e)
        return False


class _CardMsgStubV2:
    """CardV2 收尾孤儿卡时的最小消息桩。

    finalize 只用到 msg.conversation_id（重试按钮的参数），其余属性给空值即可。
    """
    conversation_id = ""
    sender_staff_id = ""
    conversation_type = ""
    robot_code = None
    text = None


def finalize_orphan_card_v2(card_id, body, footer="", failed=False) -> bool:
    """用 **v2 接口**收尾一张遗留的 v2 卡片。

    为什么必须分开发：v2 卡片的 card_id 是 outTrackId，**v1 的
    AIMarkdownCardInstance + msgContent 接口对它无效**——钉钉那边会静默失败，
    卡片就永远停在「正在输入中」。这就是 2026-10-19 上线的第二个症状
    （「一会儿空白一会儿对话显示中」里那个「显示中」）。
    """
    if not card_id or card_v2 is None:
        return False
    try:
        card = card_v2.CardV2(_CardMsgStubV2())
        card.out_track_id = card_id
        card.card_instance_id = card_id
        # 遗留卡是被中断在「流式态」里的：先关掉流式生命周期，再用 instances
        # 接口一次性写终态（blockList 只在流式态之后才生效）。
        card._stream_open = True
        ok = card.finalize(
            content=(body or "").strip() or "（重启前这一轮没有留下回答）",
            status=footer or "", failed=failed)
        return bool(ok)
    except Exception as e:  # noqa: BLE001
        logger.warning("收尾遗留 v2 卡片失败（%s）：%s", card_id, e)
        return False


def recover_inflight(client, log=None) -> int:
    """重启后找回：把上一进程没发完的卡片收尾（尽量补上真实回答）。

    v2 / v1 两种卡片要用各自的接口收尾（见 finalize_orphan_card_v2）。
    返回收尾成功的卡片数。best-effort，任何一步失败都不抛。
    """
    log = log or logger
    entries = INFLIGHT.snapshot()
    if not entries:
        return 0
    oc = OpenCodeService()
    done = 0
    for e in entries:
        sid = e.get("session_id") or ""
        started = float(e.get("started_at") or 0)
        body = ""
        try:
            m = oc.last_assistant(sid) if sid else None
            if m:
                created = float(((m.get("time") or {}).get("created") or 0)) / 1000.0
                # 只认「这一轮开始之后」才生成的回答，避免张冠李戴套上一条
                if created and started and created < started - 5:
                    m = None
            if m:
                body = OpenCodeService._text_of(m)
        except Exception as ex:  # noqa: BLE001
            log.warning("启动找回：取回答失败（%s）：%s", sid, ex)
        footer = "🔄 进程重启，本条为收尾补发"
        if e.get("card_id"):
            if e.get("card_v2"):
                ok = finalize_orphan_card_v2(e["card_id"], body, footer=footer)
            else:
                ok = finalize_orphan_card(client, e["card_id"], body, footer=footer,
                                          title=e.get("title") or "OpenCode")
            if ok:
                done += 1
                log.info("启动找回：已请求收尾孤儿卡 conv=%s card=%s kind=%s（%d 字）",
                         e.get("conv_id"), e["card_id"],
                         "v2" if e.get("card_v2") else "v1", len(body or ""))
            else:
                log.warning("启动找回：收尾请求没发出去 conv=%s card=%s",
                            e.get("conv_id"), e["card_id"])
        else:
            log.warning("启动找回：回合无卡片可用（conv=%s），跳过收尾",
                        e.get("conv_id"))
    INFLIGHT.clear()
    return done


TOOL_LABELS = {
    "bash": "执行命令", "shell": "执行命令",
    "read": "读取文件", "write": "写入文件", "edit": "修改文件",
    "glob": "查找文件", "grep": "搜索内容", "list": "列目录",
    "webfetch": "抓取网页", "websearch": "网络搜索",
    "task": "派发子任务", "question": "确认信息", "todo": "更新清单",
    "skill": "加载技能",
}


# ---------------------------------------------------------------- OpenCode 客户端
class OpenCodeService:
    """本机 OpenCode 后台服务的瘦客户端。"""

    def __init__(self, base_url=None, password=None, timeout=30):
        if not base_url:
            base_url, password = self.discover()
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        token = base64.b64encode(f"opencode:{password}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}",
                        "Content-Type": "application/json"}

    @staticmethod
    def discover(path=SERVICE_JSON):
        with open(path, encoding="utf-8") as f:
            svc = json.load(f)
        return svc["url"], svc["password"]

    def _url(self, path):
        return f"{self.base}{path}"

    def info(self):
        r = requests.get(self._url("/api/info"), headers=self.headers,
                         timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def create_session(self, directory, title=None, agent=None, model=None):
        body = {"location": {"directory": directory}}
        if title:
            body["title"] = title
        if agent:
            body["agent"] = agent
        if model:
            body["model"] = model
        r = requests.post(self._url("/api/session"), headers=self.headers,
                          json=body, timeout=self.timeout)
        r.raise_for_status()
        return r.json()["data"]

    def prompt(self, session_id, text, files=None):
        """投递一条用户消息（异步）。files: [{"uri": "file:///...", "name": "x.png"}]"""
        body = {"text": text}
        if files:
            body["files"] = files
        r = requests.post(self._url(f"/api/session/{session_id}/prompt"),
                          headers=self.headers, json=body, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def wait_idle(self, session_id, timeout=WAIT_TIMEOUT):
        r = requests.post(
            self._url(f"/api/experimental/session/{session_id}/wait"),
            headers=self.headers, timeout=timeout)
        return r.status_code == 204

    def messages(self, session_id, limit=50, order="desc"):
        r = requests.get(self._url(f"/api/session/{session_id}/message"),
                         headers=self.headers,
                         params={"limit": limit, "order": order},
                         timeout=self.timeout)
        r.raise_for_status()
        return r.json()["data"]

    def switch_model(self, session_id, provider_id, model_id, variant=None):
        model = {"providerID": provider_id, "id": model_id}
        if variant:
            model["variant"] = variant
        r = requests.post(self._url(f"/api/session/{session_id}/model"),
                          headers=self.headers, json={"model": model},
                          timeout=self.timeout)
        r.raise_for_status()
        return r.status_code

    def models(self):
        """本机可用的全部模型：[{"id","providerID",...}]。

        注意：切换模型的接口**不校验** id（不存在的 id 也返回 204），
        所以想拦住打错的模型名，只能拿这份清单自己比对。
        """
        r = requests.get(self._url("/api/model"), headers=self.headers,
                         timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("data") or []

    def session_model(self, session_id):
        """当前会话实际在用的模型：{"providerID","id","variant"} / None。"""
        r = requests.get(self._url(f"/api/session/{session_id}"),
                         headers=self.headers, timeout=self.timeout)
        r.raise_for_status()
        return (r.json().get("data") or {}).get("model")

    def set_instruction(self, session_id, key, value):
        r = requests.put(
            self._url(f"/api/experimental/session/{session_id}/instructions/entries/{key}"),
            headers=self.headers, json={"value": value}, timeout=self.timeout)
        r.raise_for_status()
        return r.status_code

    def list_instructions(self, session_id):
        r = requests.get(
            self._url(f"/api/experimental/session/{session_id}/instructions/entries"),
            headers=self.headers, timeout=self.timeout)
        r.raise_for_status()
        return r.json()["data"]

    # ---- 流式 / 会话控制（V2 API）----
    def stream(self, path, timeout=(10, 300)):
        """流式 GET（SSE）：调用方负责设 r.encoding、迭代与关闭。"""
        return requests.get(self._url(path), headers=self.headers, stream=True,
                            timeout=timeout)

    def interrupt(self, session_id, resume=False):
        """打断正在跑的这一轮（/stop 用）。返回 True 表示确实打断了。"""
        r = requests.post(self._url(f"/api/session/{session_id}/interrupt"),
                          headers=self.headers,
                          params={"resume": "true" if resume else "false"},
                          timeout=self.timeout)
        r.raise_for_status()
        return bool((r.json() or {}).get("interrupted"))

    def compact(self, session_id):
        """请求压缩上下文（默认在下一个 step 边界执行）。"""
        r = requests.post(self._url(f"/api/session/{session_id}/compact"),
                          headers=self.headers, json={}, timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("data") or {}

    def fork(self, session_id, before=None):
        """分叉出一个子会话（复制历史），返回新会话信息。"""
        body = {"before": before} if before else {}
        r = requests.post(self._url(f"/api/session/{session_id}/fork"),
                          headers=self.headers, json=body, timeout=self.timeout)
        r.raise_for_status()
        return r.json()["data"]

    def permissions(self, session_id):
        """列出该会话里**等待客户端应答**的权限请求。"""
        r = requests.get(self._url(f"/api/session/{session_id}/permission"),
                         headers=self.headers, timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("data") or []

    def reply_permission(self, session_id, request_id, decision, message=None):
        """应答权限请求：decision ∈ once / always / reject。"""
        body = {"decision": decision}
        if message:
            body["message"] = message
        r = requests.post(
            self._url(f"/api/session/{session_id}/permission/{request_id}/reply"),
            headers=self.headers, json=body, timeout=self.timeout)
        r.raise_for_status()
        return r.status_code

    def forms(self, session_id):
        """列出等待应答的表单（question 工具会落成 kind=question 的表单）。"""
        r = requests.get(self._url(f"/api/session/{session_id}/form"),
                         headers=self.headers, timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("data") or []

    def reply_form(self, session_id, form_id, answer):
        """应答表单：answer 是 {字段key: 值} 映射。"""
        r = requests.post(
            self._url(f"/api/session/{session_id}/form/{form_id}/reply"),
            headers=self.headers, json={"answer": answer}, timeout=self.timeout)
        r.raise_for_status()
        return r.status_code

    # ---- 便捷 ----
    @staticmethod
    def _text_of(msg):
        out = []
        for part in msg.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "text":
                out.append(part.get("text") or "")
        return "".join(out).strip()

    def last_assistant(self, session_id):
        for msg in self.messages(session_id, order="desc"):
            if msg.get("type") == "assistant":
                return msg
        return None


# ---------------------------------------------------------------- 事件流（SSE）
class EventBus:
    """进程内单例：订阅本机 OpenCode 事件流（GET /api/event，SSE），按 sessionID 分发。

    为什么需要它：原实现每 0.6s 拉一次 /message 才能知道「回答生成到哪了」，
    既慢（首字延迟一拍以上）又浪费请求。事件流能直接给出
    `session.text.delta`（逐字）和 `session.tool.*` 生命周期。

    注意事件流是**易失**的（官方文档：慢消费者会撑爆流、断线期间事件会丢），
    所以它只用来**加速**：落后或断流时由调用方的兜底轮询补齐。
    """

    _instance = None
    _instance_lock = threading.Lock()

    @classmethod
    def instance(cls) -> "EventBus":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self):
        self._subs: dict[str, set[queue.Queue]] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._last_error = ""

    # ---- 订阅 ----
    def subscribe(self, session_id: str, maxsize: int = 500) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subs.setdefault(session_id, set()).add(q)
        self._ensure_thread()
        return q

    def unsubscribe(self, session_id: str, q: queue.Queue) -> None:
        with self._lock:
            subs = self._subs.get(session_id)
            if subs:
                subs.discard(q)
                if not subs:
                    self._subs.pop(session_id, None)

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def last_error(self) -> str:
        return self._last_error

    def _ensure_thread(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="oc-event-bus",
                                        daemon=True)
        self._thread.start()

    def _publish(self, ev: dict):
        sid = (ev.get("data") or {}).get("sessionID")
        if not sid:
            return
        with self._lock:
            queues = list(self._subs.get(sid) or ())
        for q in queues:
            try:
                q.put_nowait(ev)
            except queue.Full:
                # 消费者跟不上：丢最旧的，保证最新状态能进去（事件是易失的，
                # 完整性由兜底轮询保证）
                try:
                    q.get_nowait()
                    q.put_nowait(ev)
                except (queue.Empty, queue.Full):
                    pass

    def _run(self):
        attempt = 0
        while not self._stop.is_set():
            try:
                self._pump()
            except Exception as e:  # noqa: BLE001
                self._connected.clear()
                self._last_error = f"{type(e).__name__}: {e}"
                logger.info("事件流断开（%s）", self._last_error)
            else:
                attempt = 0
            if self._stop.is_set():
                return
            delay = EVENT_RECONNECT_DELAYS[min(attempt, len(EVENT_RECONNECT_DELAYS) - 1)]
            attempt += 1
            self._stop.wait(delay)

    def _pump(self):
        oc = OpenCodeService()
        r = oc.stream("/api/event")
        try:
            # 不显式设 utf-8 的话 requests 会按 latin-1 解码，中文全乱码
            r.encoding = "utf-8"
            self._connected.set()
            logger.info("事件流已连接（/api/event）")
            for line in r.iter_lines(decode_unicode=True):
                if self._stop.is_set():
                    return
                if not line or not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if not body:
                    continue
                try:
                    self._publish(json.loads(body))
                except json.JSONDecodeError:
                    continue
        finally:
            self._connected.clear()
            try:
                r.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------- 卡片写入
class CardWriter:
    """钉钉 AI 卡片写入器：防抖 + 全局限流 + 退避重试 + 超长截断。

    钉钉约束（AI 卡片流式更新文档）：markdown 必须**全量**更新（isFull=true），
    单次内容建议 ≤1KB、总大小 ≤10KB；接口本身有 QPS 限制，多会话叠加容易
    返回 403 QpsLimit。官方连接器的做法是 800ms 防抖 + 全局令牌桶 + 退避重试，
    这里照抄该策略，并把「一次都没发出去」的风险压到最低。
    """

    _slot_lock = threading.Lock()
    _slot_ts = 0.0

    def __init__(self, handler, msg, title="OpenCode",
                 min_interval=CARD_MIN_INTERVAL):
        self.handler = handler
        self.msg = msg
        self.title = title
        self.min_interval = min_interval
        self.card = None
        self.notes: list[str] = []          # 页脚上的附加提示（放行/拒绝/中断）
        self.truncated = False
        self._last_ts = 0.0
        self._last_text = None
        self._last_footer = None

    # ---- 全局限流 ----
    @classmethod
    def _wait_slot(cls, qps=CARD_QPS):
        gap = 1.0 / max(int(qps), 1)
        with cls._slot_lock:
            now = time.time()
            wait = cls._slot_ts + gap - now
            if wait > 0:
                time.sleep(wait)
                now = time.time()
            cls._slot_ts = now

    @staticmethod
    def _is_throttle_error(e: Exception) -> bool:
        s = str(e)
        return ("QpsLimit" in s or "qps" in s.lower() or "403" in s
                or "限流" in s or "rate" in s.lower())

    def _call(self, fn, *args, **kwargs):
        """带全局限流 + 限流退避重试的卡片调用。失败只记日志，不往上抛。"""
        for attempt in (0, 1):
            self._wait_slot()
            try:
                return fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                if attempt == 0 and self._is_throttle_error(e):
                    logger.warning("卡片接口被限流，%ss 后重试：%s",
                                   CARD_RETRY_DELAY, e)
                    time.sleep(CARD_RETRY_DELAY)
                    continue
                logger.warning("卡片调用失败：%s", e)
                return None
        return None

    # ---- 生命周期 ----
    def start(self) -> bool:
        """创建 AI 卡片并进入「输入中」。失败返回 False（调用方降级为 markdown）。"""
        try:
            card = AIMarkdownCardInstance(self.handler.dingtalk_client, self.msg)
            card.set_title_and_logo(self.title, "")
            card.ai_start()
        except Exception as e:  # noqa: BLE001
            logger.warning("创建 AI 卡片失败：%s", e)
            return False
        if not card.card_instance_id:
            return False
        self.card = card
        self._call(card.ai_streaming, "正在思考…", append=False)
        return True

    def _clip(self, text: str) -> str:
        raw = (text or "").encode("utf-8")
        if len(raw) <= CARD_MAX_BYTES:
            return text
        self.truncated = True
        return raw[:CARD_MAX_BYTES - 24].decode("utf-8", "ignore") + "\n\n…（已截断）"

    def stream(self, text: str, force=False):
        """更新正文。默认防抖（CARD_MIN_INTERVAL 内不重复发）。"""
        if not self.card:
            return
        text = (text or "").strip()
        if not text or text == self._last_text:
            return
        now = time.time()
        if not force and now - self._last_ts < self.min_interval:
            return
        self._last_ts = now
        self._last_text = text
        self._call(self.card.ai_streaming, self._clip(text), append=False)

    def footer(self, text: str, force=False):
        """更新卡片上的静态区域（工具链 / 耗时 / 提示）。"""
        if not self.card or not text or text == self._last_footer:
            return
        if not force and time.time() - self._last_ts < self.min_interval:
            return
        self._last_footer = text
        self.card.static_markdown = text
        self._call(self.card.put_card_data, self.card.card_instance_id,
                   self.card.get_card_data(AICardStatus.INPUTING))

    def note(self, text: str):
        """追加一条页脚提示（去重、最多 3 条）。"""
        if text and text not in self.notes:
            self.notes.append(text)
            del self.notes[:-3]

    def footer_text(self, parts, elapsed=None, model=None, status=None):
        return render_footer(parts, elapsed=elapsed, model=model, notes=self.notes,
                             status=status)

    def finish(self, text: str, footer="", failed=False, parts=None) -> bool:
        if not self.card:
            return False
        text = self._clip((text or "").strip() or "（空回复）")
        self.card.markdown = text
        self.card.static_markdown = footer
        self._call(self.card.streaming, self.card.card_instance_id, "msgContent",
                   text, append=False, finished=True, failed=failed)
        self._call(self.card.finish, self.card.card_instance_id,
                   self.card.get_card_data())
        if failed:
            self._call(self.card.ai_fail)
        return True


# ---------------------------------------------------------------- 卡片回调登记表
class CardActionRegistry:
    """卡片按钮回调 → 等待中的线程 的登记表。

    钉钉卡片回调走 Stream 长连接（topic `/v1.0/card/instances/callback`），
    在**另一个线程**里到达；而需要这个答案的是当前这一轮正在跑的线程。
    所以用「会话 → Event + 结果」把两边接起来。

    只保留最新一条待处理项：同一会话里旧的回调不应该结算新的等待。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._waiting = {}       # conv -> {"event": Event, "action": ..., "params": ...}

    def arm(self, conv):
        """在开始等待前调用，登记一个空的等待位。"""
        ev = threading.Event()
        with self._lock:
            self._waiting[conv] = {"event": ev, "action": None, "params": {}}
        return ev

    def resolve(self, conv, action, params):
        """卡片回调到达时调用。命中返回 True。"""
        with self._lock:
            rec = self._waiting.get(conv)
            if not rec or rec["event"].is_set():
                return False
            rec["action"] = action
            rec["params"] = params or {}
            rec["event"].set()
            return True

    def wait(self, conv, timeout):
        """等用户点按钮。返回 (action, params)，超时返回 (None, {})。"""
        with self._lock:
            rec = self._waiting.get(conv)
            ev = rec["event"] if rec else None
        if ev is None:
            return None, {}
        if not ev.wait(timeout):
            with self._lock:
                self._waiting.pop(conv, None)
            return None, {}
        with self._lock:
            rec = self._waiting.pop(conv, {}) or {}
        return rec.get("action"), (rec.get("params") or {})

    def clear(self, conv):
        with self._lock:
            rec = self._waiting.pop(conv, None)
        if rec:
            rec["event"].set()


CARD_ACTIONS = CardActionRegistry()


class LastPromptStore:
    """记住每个会话最近一次的**用户原话**，供卡片上的「重试」按钮重放。

    只存文本，不存附件——重试重放的是一条文本提问，这一点在按钮语义上已足够
    （附件重放容易引发重复上传/重复计费）。落盘一次，重启后仍可重试。
    """

    def __init__(self, path=None):
        self.path = path or os.path.join(RUNTIME_DIR, "last_prompt.json")
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            return d if isinstance(d, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def remember(self, conv, text):
        text = (text or "").strip()
        if not conv or not text:
            return
        with self._lock:
            self._data[conv] = {"text": text[:4000], "at": time.time()}
            # 只保留最近 50 个会话，避免文件无限增长
            if len(self._data) > 50:
                for k in sorted(self._data, key=lambda c: self._data[c].get("at", 0))[:10]:
                    self._data.pop(k, None)
            try:
                _atomic_write_json(self.path, self._data)
            except Exception:  # noqa: BLE001
                pass

    def get(self, conv):
        with self._lock:
            return (self._data.get(conv) or {}).get("text") or ""


LAST_PROMPT = LastPromptStore()


# ------------------------------------------------------------ 卡片问答档案
# 用户「引用」一张历史卡片回复时，钉钉下发的被引用内容只是那条消息的一小段
# 文本（卡片消息常常只有概览级文字），拿不到整段回答；单聊里更没有消息历史
# API 可查（见 dingtalk-stream-sdk-python#49）。想真正"回到那张卡的上下文"，
# 只能自己留档：每轮回答发完时记下「卡片 id → 提问 + 完整回答」，引用发生时
# 按卡片 id 或文本匹配找回，再把完整问答注入 prompt（详见 build_quote_context）。
CARD_ARCHIVE_FILE = os.path.join(RUNTIME_DIR, "card_archive.json")
CARD_ARCHIVE_KEEP = 200          # 全库最多留多少张卡（超出淘汰最旧）
CARD_ARCHIVE_PER_CONV = 30       # 单个会话最多留多少张卡
CARD_MATCH_MIN = 0.60            # 文本匹配阈值，低于此判定"没匹配上"


def _norm_text(s: str) -> str:
    """归一化用于文本匹配：去掉空白与 markdown 装饰符，全部小写。

    钉钉回传的引用摘要会压扁换行/缩进，卡片正文又带 `**`、`>`、`-` 这些
    装饰符，不归一化的话同一段话比对不上。
    """
    if not s:
        return ""
    return re.sub(r"[\s\*`>#\-\[\]\(\)\|:：,，。;；!！?？\"'“”‘’…]+", "",
                  str(s)).lower()


def _similarity(q: str, t: str) -> float:
    """两段已归一化文本的相似度 0~1。

    互相包含时直接给高分（长边越接近越接近 1）；否则用 SequenceMatcher
    在前 200 字上做近似——够用且便宜（档案最多 200 条）。
    """
    if not q or not t:
        return 0.0
    if q in t or t in q:
        shorter, longer = (q, t) if len(q) <= len(t) else (t, q)
        return 0.75 + 0.25 * (len(shorter) / max(1, len(longer)))
    return difflib.SequenceMatcher(None, q[:200], t[:200]).ratio()


class CardArchive:
    """已发卡片的「问答档案」：卡片 id / outTrackId → 该轮的提问 + 最终回答。

    结构：{"cards": {key: rec}, "order": [key, ...]}，key 优先 card_instance_id。
    rec = {conv, card_id, out_track_id, session_id, question, answer, at}
    落盘是为了跨重启可用（重启会清空对话上下文，档案不能再跟着没）。
    """

    def __init__(self, path=None):
        self.path = path or CARD_ARCHIVE_FILE
        self._lock = threading.Lock()
        self._data = self._load()

    # ---- 读写 ----
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            if isinstance(d, dict) and isinstance(d.get("cards"), dict):
                return {"cards": d["cards"], "order": list(d.get("order") or [])}
        except Exception:  # noqa: BLE001
            pass
        return {"cards": {}, "order": []}

    def remember(self, conv, question, answer, card_id="", out_track_id="",
                 session_id=""):
        answer = (answer or "").strip()
        key = (card_id or out_track_id or "").strip()
        if not conv or not key or not answer:
            return
        rec = {"conv": conv,
               "card_id": card_id or "",
               "out_track_id": out_track_id or "",
               "session_id": session_id or "",
               "question": (question or "").strip()[:4000],
               "answer": answer[:20000],
               "at": time.time()}
        with self._lock:
            cards = self._data["cards"]
            cards[key] = rec
            self._data["order"] = [k for k in self._data["order"] if k != key]
            self._data["order"].append(key)
            self._trim(conv)
            try:
                _atomic_write_json(self.path, self._data)
            except Exception:  # noqa: BLE001
                pass

    def _trim(self, conv):
        """淘汰：先按会话限条数，再全局限条数。调用方须持有 self._lock。"""
        cards = self._data["cards"]
        order = [k for k in self._data["order"] if k in cards]
        mine = [k for k in order if (cards.get(k) or {}).get("conv") == conv]
        if len(mine) > CARD_ARCHIVE_PER_CONV:
            for k in mine[:-CARD_ARCHIVE_PER_CONV]:
                cards.pop(k, None)
        order = [k for k in order if k in cards]
        while len(order) > CARD_ARCHIVE_KEEP:
            cards.pop(order.pop(0), None)
        self._data["order"] = order

    # ---- 查找 ----
    def recent(self, conv, limit=CARD_ARCHIVE_PER_CONV):
        """本会话最近的卡片，新的在前。"""
        with self._lock:
            cards, order = self._data["cards"], self._data["order"]
            recs = [cards[k] for k in reversed(order)
                    if (cards.get(k) or {}).get("conv") == conv]
        return recs[:limit]

    def by_id(self, key):
        """按卡片 id / outTrackId 精确取；找不到再线性扫一遍。"""
        key = (key or "").strip()
        if not key:
            return None
        with self._lock:
            cards = self._data["cards"]
            rec = cards.get(key)
            if rec:
                return rec
            for r in cards.values():
                if key in (r.get("card_id"), r.get("out_track_id")):
                    return r
        return None

    def match_text(self, conv, quoted_text, limit=CARD_ARCHIVE_PER_CONV):
        """按被引用内容做文本匹配（钉钉只给摘要时的主路径）。

        返回 (rec, score)；没到阈值返回 (None, 最高分)。
        """
        q = _norm_text(quoted_text)
        best, best_score = None, 0.0
        if len(q) < 6:              # 太短（"OpenCode"这类概览文字）不猜
            return None, 0.0
        for rec in self.recent(conv, limit):
            score = max(_similarity(q, _norm_text(rec.get("question"))),
                        _similarity(q, _norm_text(rec.get("answer"))))
            if score > best_score:
                best, best_score = rec, score
        return (best if best_score >= CARD_MATCH_MIN else None), best_score


CARD_ARCHIVE = CardArchive()

# 卡片消息的 outTrackId 形如 oc-<20位hex>；钉钉回传的引用内容里若带上了它，
# 就能精确命中档案（比文本匹配可靠），所以顺手扫一遍。
_OUT_TRACK_RE = re.compile(r"oc-[0-9a-zA-Z]{12,}")


def build_quote_context(quote, conv, max_answer=6000, log=None):
    """把「引用回复」拼成一段注入 agent 的上下文。返回 "" 表示没引用。

    三级定位（越靠前越可靠）：
      1. 引用内容里带 outTrackId → 档案精确命中；
      2. 被引用文本与档案里的提问/回答做文本匹配；
      3. 都失败但本会话有卡片 → 退回最近一张卡，并在文里**注明是降级匹配**。
    第 3 级是刻意的：钉钉给的摘要有时短到没有特征（只剩"OpenCode"这种
    卡片概览），此时"最近那张卡"通常就是用户想指的那张；但必须让 agent
    知道这是猜的，回答里要留余地。
    """
    log = log or logger
    quote = quote or {}
    qtext = (quote.get("text") or "").strip()
    if not quote.get("is_reply") and not qtext:
        return ""

    rec, how = None, ""
    raw = json.dumps(quote.get("raw") or {}, ensure_ascii=False)
    for key in dict.fromkeys([quote.get("card_id") or ""]
                             + [quote.get("msg_id") or ""]
                             + _OUT_TRACK_RE.findall(raw)):
        rec = CARD_ARCHIVE.by_id(key)
        if rec:
            how = f"卡片 id 精确命中（{key[:40]}）"
            break
    if not rec and qtext:
        rec, score = CARD_ARCHIVE.match_text(conv, qtext)
        if rec:
            how = f"文本匹配命中（相似度 {score:.2f}）"
        else:
            log.info("引用内容没能匹配到历史卡片（最高相似度 %.2f）", score)
    if not rec:
        recs = CARD_ARCHIVE.recent(conv, limit=1)
        if recs:
            rec, how = recs[0], "降级：会话内最近一张卡片（未能精确匹配）"

    if not rec:
        return ("【用户引用了某条历史消息】\n"
                f"钉钉下发的被引用内容：\n{qtext or '（无文字内容）'}\n\n"
                "（本机没有这条被引用消息的档案——可能是引用别人/别的应用的消息，"
                "或者是本机型首次运行后清过档案。若判断需要原文，直接向用户确认。）")

    ans = (rec.get("answer") or "").strip()
    if len(ans) > max_answer:
        ans = ans[:max_answer] + "\n…（档案回答过长，已截断）"
    log.info("引用回复已接入历史卡片：%s card=%s 提问%d字 回答%d字",
             how, (rec.get("card_id") or "")[:24],
             len(rec.get("question") or ""), len(ans))
    return ("【用户引用了一张历史卡片，档案已自动载入】\n"
            f"（定位方式：{how}）\n"
            f"钉钉下发的被引用内容：\n{qtext or '（无文字内容）'}\n\n"
            f"— 该卡片对应的原始提问 —\n{rec.get('question') or '（未记录）'}\n\n"
            f"— 该卡片的完整回答 —\n{ans}\n\n"
            "（以上是历史上下文，供你准确理解用户在指什么；不要照抄，"
            "只回答用户这次的新要求。）")


# 「重试」按钮的实际执行由监听进程注入（它才持有 handler / 并发闸门）
_RETRY_HOOK = None


def set_retry_hook(fn):
    global _RETRY_HOOK
    _RETRY_HOOK = fn


def _journal_feedback(kind, conv, session_id, extra=""):
    """把用户反馈写进 append-only 的 journal（改进扫描的证据来源）。

    retry / stop 是**最强的负反馈信号**（比"这不太对"这种含糊表达强得多），
    所以优先记它们。写失败绝不影响主流程。
    """
    try:
        from tools import memory_store as ms
        ms.append_journal(
            kind, f"用户反馈：{kind}",
            conv=conv or "-", session=session_id or "-",
            detail=extra or "-")
    except Exception as e:  # noqa: BLE001
        logger.warning("journal 写入失败：%s", e)


def _handle_evolve_proposal(conv, action, params, log=None, out_track=""):
    """处理改进提案审批卡的回调（**无状态**：提案 id 编在 question_id 里）。

    evolve 作业是 scheduler 的独立子进程，推审批卡时 bot 进程里没有等待槽，
    所以这里按提案 id 直接读 memory/proposals/<id>.json 落地/驳回。
    这样即使 bot 中途重启，审批也照样能处理。
    """
    log = log or logger
    qid = ""
    for src in (action or "", (params or {}).get("question_id") or "",
                (params or {}).get("questionId") or ""):
        if isinstance(src, str) and src.startswith("evolve:"):
            qid = src.strip()
            break
    if not qid:
        return False
    pid = qid.split("evolve:", 1)[1].strip()
    if not pid:
        return False

    try:
        from tools import card_v2 as _cv
        from tools import memory_store as _ms
        from tools import apply_proposal as _ap
    except Exception as e:  # noqa: BLE001
        log.warning("改进提案处理：导入失败 %s", e)
        return False

    answers = _cv.parse_ask_answer(params) or {}
    decision = answers.get("decision")
    if isinstance(decision, list):
        decision = decision[0] if decision else ""
    decision = (decision or "").strip()
    if (params or {}).get("user_cancel"):
        decision = "defer"

    def _finish(status, summary):
        """回写卡片状态 —— 卡片是别的进程推的，bot 进程没有它的对象，
        但更新接口只要 outTrackId。不回写的话卡片会一直停在「待提交」，
        用户点了像没反应。"""
        try:
            _cv.finish_ask_card(out_track, status, summary)
        except Exception as e:  # noqa: BLE001
            log.warning("改进提案卡片状态回写失败：%s", e)

    try:
        if decision == "approve":
            ok, msg = _ap.approve(pid)
            _finish(_cv.ASK_STATUS_SUBMITTED if ok else _cv.ASK_STATUS_CANCELLED,
                    ("✅ 已批准并落地：%s" % msg) if ok else ("❌ 批准失败：%s" % msg))
        elif decision == "reject":
            ok, msg = _ap.reject(pid, note="用户在钉钉审批卡上驳回")
            _finish(_cv.ASK_STATUS_CANCELLED,
                    "🚫 已驳回：%s" % msg if ok else "❌ 驳回失败：%s" % msg)
        elif decision == "defer":
            ok, msg = _ap.defer(pid)
            _finish(_cv.ASK_STATUS_EXPIRED, "⏸ 已暂缓：%s" % msg)
        else:
            log.info("改进提案：无法识别的决定 %r conv=%s pid=%s",
                     decision, conv, pid)
            _finish(_cv.ASK_STATUS_CANCELLED, "❓ 没看懂你的选择，提案保持待审批")
            return False
    except Exception as e:  # noqa: BLE001
        log.warning("改进提案处理失败 pid=%s: %s", pid, e)
        _finish(_cv.ASK_STATUS_CANCELLED, "❌ 处理出错：%s" % e)
        return False

    log.info("改进提案 %s：%s（决定=%s）", pid, msg, decision or "-")
    return bool(ok)


def handle_card_action(conv, action, params, log=None):
    """卡片按钮的统一入口（Stream 卡片回调处理器调用）。

    - ask   ：结算正在等答案的提问卡
    - stop  ：中断当前这一轮
    - retry ：重放最近一次提问
    - evolve:xxx ：改进提案的审批（跨进程，无状态处理）
    返回 True 表示已处理。
    """
    log = log or logger
    action = (action or "").strip()
    # 表单提交：提问卡的回传没有固定 actionId（模板会把 question_id 当 actionId
    # 回来），只要带表单答案就算作答。
    if params and (params.get("form") or params.get("user_cancel")):
        # 改进提案审批卡是**跨进程**的（evolve 作业是独立子进程，推卡时
        # bot 进程里没有等待槽），所以必须先按 question_id 里的提案 id
        # 做无状态处理，不能直接丢给 CARD_ACTIONS。
        if _handle_evolve_proposal(conv, action, params, log=log):
            return True
        return CARD_ACTIONS.resolve(conv, action or "ask", params)
    if not action:
        return False

    if action in ("ask", "ask_text"):
        return CARD_ACTIONS.resolve(conv, action, params)

    # 卡片内置「停止」按钮（模板固定 actionId = btn_stop）
    if action in ("stop", "_stop", "btn_stop", "interrupt"):
        try:
            sid = SessionMap(os.path.join(RUNTIME_DIR, "sessions.json")).get(conv)
            if not sid:
                log.info("卡片停止：会话不存在 conv=%s", conv)
                return False
            hit = OpenCodeService().interrupt(sid)
            log.info("卡片停止：conv=%s sid=%s hit=%s", conv, sid, hit)
            _journal_feedback("stop", conv, sid, extra="用户中断了这一轮")
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("卡片停止失败：%s", e)
            return False

    if action in ("retry", "again"):
        text = LAST_PROMPT.get(conv)
        if not text:
            log.info("卡片重试：没有记住的提问 conv=%s", conv)
            return False
        if _RETRY_HOOK is None:
            log.warning("卡片重试：未注入重试钩子")
            return False
        try:
            _RETRY_HOOK(conv, text)
            log.info("卡片重试已触发：conv=%s", conv)
            _journal_feedback("retry", conv, "", extra=f"用户要求重做：{text[:120]}")
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("卡片重试失败：%s", e)
            return False

    log.info("卡片动作未识别：%s %s", action, params)
    return False


def resolve_card_action(conv, action, params) -> bool:
    """兼容旧入口（等价于 handle_card_action）。"""
    return handle_card_action(conv, action, params)


# ---------------------------------------------------------------- /model 切换
def model_name(model):
    """把 /api/session 里的 model 字段渲染成一行短文本（给用户看）。"""
    if not model:
        return "（默认）"
    pid = model.get("providerID") or ""
    mid = model.get("id") or ""
    for c in MODEL_CHOICES:
        if c["id"] == mid and _choice_provider(c) == pid:
            return f"{c['label']}（`{mid}`）"
    return f"`{mid}`（{pid}）"


def _choice_provider(choice):
    return (choice.get("provider") or choice.get("key") or "").split("|", 1)[0]


def resolve_model_ref(ref):
    """用户输入 → 候选模型。支持：序号 1/2/3、别名、模型 id、provider/model。

    返回 {"key","label","id","provider"}；认不出来的原样带回（交给清单校验去拦）。
    """
    raw = (ref or "").strip()
    if not raw:
        raise ValueError("要切哪个模型？发 `/model` 点选。")
    low = raw.lower().replace(" ", "")
    if low in ("1", "2", "3"):
        return dict(MODEL_CHOICES[int(low) - 1])
    for c in MODEL_CHOICES:
        if low in (c["id"].lower(), c["key"].lower(),
                   c["label"].lower().replace(" ", "")):
            return dict(c)
    provider = ""
    mid = raw
    if "|" in raw:
        provider, mid = [s.strip() for s in raw.split("|", 1)]
    elif "/" in raw:
        provider, mid = [s.strip() for s in raw.split("/", 1)]
    return {"key": "", "label": mid, "id": mid, "provider": provider}


def switch_model_checked(session_id, ref, log=None):
    """按用户输入切模型，成功返回一句给用户看的话，失败抛异常。

    为什么要自己校验：切换接口**不校验 id**（不存在的模型也返回 204），
    不比对 `/api/model` 清单的话，打错的名字也会显示「切换成功」，
    下一轮却整轮报 `Model unavailable`。
    """
    log = log or logger
    oc = OpenCodeService()
    choice = resolve_model_ref(ref)
    mid = choice["id"]
    provider = (choice.get("provider") or "").strip()

    try:
        avail = oc.models()
    except Exception as e:  # noqa: BLE001
        avail = []
        log.warning("取模型清单失败，跳过存在性校验：%s", e)
    pairs = {(m.get("providerID"), m.get("id")) for m in avail}

    if not avail:
        # 清单拿不到：只拦「完全没给 provider」的情况，别因接口抖动把用户挡死
        if not provider:
            raise RuntimeError("取不到模型清单，没法确认这个 id；发 `/model` 点选试试")
    elif not provider:
        holders = [p for (p, i) in pairs if i == mid]
        if not holders:
            raise ValueError(f"没有 `{mid}` 这个模型。发 `/model` 点选可用的三个。")
        preferred = [p for p in holders if p == DEFAULT_MODEL["providerID"]]
        provider = (preferred or holders)[0]
    elif (provider, mid) not in pairs:
        holders = [p for (p, i) in pairs if i == mid]
        if holders:
            raise ValueError(
                f"`{mid}` 属于 {' / '.join(holders)}，不是 `{provider}`。"
                f"改成 `{holders[0]}/{mid}` 再试。")
        raise ValueError(f"`{provider}/{mid}` 不存在。发 `/model` 点选可用的三个。")

    oc.switch_model(session_id, provider, mid)
    cur = oc.session_model(session_id) or {}
    if cur and (cur.get("id") != mid or cur.get("providerID") != provider):
        raise RuntimeError(
            f"回读不一致：现在是 {cur.get('providerID')}/{cur.get('id')}")
    log.info("模型已切换：%s/%s sid=%s", provider, mid, session_id)
    return f"✅ 已切换为 **{choice['label']}**（`{mid}`）"


def _await_model_choice(card, wait_key, session_id, log, timeout):
    """后台等用户点选，然后切模型并把卡片收成终态（带结果回显）。"""
    try:
        action, params = CARD_ACTIONS.wait(wait_key, timeout)
        if not action:
            card.finish(card_v2.ASK_STATUS_EXPIRED, "⌛ 超时没选，模型未改动")
            log.info("模型提问卡超时未作答（%.0fs）", timeout)
            return
        if params.get("user_cancel"):
            card.finish(card_v2.ASK_STATUS_CANCELLED, "已取消，模型未改动")
            log.info("模型提问卡被取消")
            return
        answers = card_v2.parse_ask_answer(params) or {}
        picked = answers.get("model")
        if isinstance(picked, list):
            picked = picked[0] if picked else None
        if not picked:
            card.finish(card_v2.ASK_STATUS_CANCELLED, "没收到选择，模型未改动")
            return
        hit = next((c for c in MODEL_CHOICES
                    if str(picked) in (c["key"], c["id"], c["label"])), None)
        if not hit:
            card.finish(card_v2.ASK_STATUS_CANCELLED, f"不认识的选择：{picked}")
            log.warning("模型提问卡收到未知名目：%r", picked)
            return
        try:
            line = switch_model_checked(session_id, hit["key"], log=log)
        except Exception as e:  # noqa: BLE001
            card.finish(card_v2.ASK_STATUS_CANCELLED, f"❌ 切换失败：{e}")
            log.warning("模型提问卡切换失败：%s", e)
            return
        card.finish(card_v2.ASK_STATUS_SUBMITTED, line)
        log.info("模型提问卡作答完成：%s", line)
    except Exception as e:  # noqa: BLE001
        log.warning("模型提问卡处理异常：%s", e)


def start_model_choice(msg, session_id, current=None, log=None, timeout=None):
    """投放「切换模型」提问卡（3 个选项，点选 + 提交即切）。

    返回 True 表示卡片已投放（结果在后台线程结算）；False 表示没有卡片能力
    （提问卡没开 / 投放失败），调用方应回退成文字列表。
    """
    log = log or logger
    if not (CARD_V2_OK and card_v2 is not None
            and getattr(card_v2, "ASK_CARD_ENABLED", False)):
        return False
    conv = str(getattr(msg, "conversation_id", "") or "")
    if not conv:
        return False
    fields = [card_v2.ask_field(
        "model", "选一个模型",
        [{"label": c["label"], "value": c["key"]} for c in MODEL_CHOICES])]
    if not fields or not fields[0]:
        return False
    timeout = timeout or MODEL_CHOICE_TIMEOUT
    qid = "model_" + uuid.uuid4().hex[:10]
    card = card_v2.AskCard(msg, qid, title="切换模型",
                           desc=f"当前：{current or '（默认）'}\n选好点「切换」",
                           fields=fields, submit_text="切换")
    if not card.create():
        log.warning("模型提问卡投放失败，回退文字列表")
        return False
    # 独立等待槽：别和 agent 正在用的提问卡抢同一个会话位
    wait_key = conv + "|model"
    register_card_owner(card.out_track_id, wait_key)
    CARD_ACTIONS.arm(wait_key)
    threading.Thread(target=_await_model_choice,
                     args=(card, wait_key, session_id, log, timeout),
                     daemon=True).start()
    log.info("模型提问卡已投放 qid=%s outTrackId=%s（%.0fs 内等点选）",
             qid, card.out_track_id, timeout)
    return True



# ---------------------------------------------------------------- 卡片归属登记
# 钉钉卡片回调只给 outTrackId + userId，**不给 conversation_id**，
# 所以自己维护 outTrackId -> conversation_id 的映射，回调才能找回会话。
_CARD_OWNER = {}
_CARD_OWNER_LOCK = threading.Lock()


def register_card_owner(out_track_id, conv):
    if not (out_track_id and conv):
        return
    with _CARD_OWNER_LOCK:
        _CARD_OWNER[out_track_id] = conv
        if len(_CARD_OWNER) > 500:          # 有界，防止长跑无限增长
            for k in list(_CARD_OWNER)[:100]:
                _CARD_OWNER.pop(k, None)


def conv_of_card(out_track_id):
    with _CARD_OWNER_LOCK:
        return _CARD_OWNER.get(out_track_id or "", "")


def dispatch_card_callback(data, log=None) -> bool:
    """处理一条钉钉卡片回调原始数据（data 即 CallbackMessage.data）。

    返回 True 表示这次回调被本次调用结算掉了。
    """
    log = log or logger
    try:
        payload = data.get("content") if isinstance(data, dict) else None
        out_track = ((data or {}).get("outTrackId")
                     or (card_v2.extract_out_track_id(data) if card_v2 else ""))
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:  # noqa: BLE001
                payload = {}
        conv = conv_of_card(out_track)
        action, params = parse_card_action(data if isinstance(data, dict) else payload)
        log.info("卡片回调：outTrackId=%s conv=%s action=%s params=%s",
                 out_track, conv, action,
                 json.dumps(params, ensure_ascii=False)[:400])
        if not action and not params:
            # 解析不出动作时把原文打出来——按钮/表单回传的报文形状不唯一，
            # 有了这条日志才能一次定位（而不是猜）。
            log.warning("卡片回调无法解析动作，原始载荷：%s",
                        json.dumps(data, ensure_ascii=False)[:800])
        # 改进提案审批卡是**跨进程**推的（evolve 作业是独立子进程），bot 进程的
        # outTrackId→conv 内存表里没有它。它的审批信息全编码在提案 id 里，
        # 所以必须在 conv 查不到之前就处理掉。
        if _handle_evolve_proposal(conv, action, params, log=log, out_track=out_track):
            return True
        if not conv:
            log.warning("卡片回调找不到归属会话（outTrackId=%s），忽略", out_track)
            return False
        return handle_card_action(conv, action, params, log=log)
    except Exception as e:  # noqa: BLE001
        log.warning("处理卡片回调失败：%s", e)
        return False


def parse_card_action(payload):
    """薄封装：转发到 card_v2 的解析器（模块缺失时返回空）。"""
    if card_v2 is None:
        return None, {}
    return card_v2.parse_card_action(payload)


# ---------------------------------------------------------------- v2 卡片写入器
# 是否启用 v2 链路：需要 card_v2 模块可用 + 环境变量开关。
CARD_V2_OK = bool(card_v2 and getattr(card_v2, "V2_ENABLED", False))
# 长任务进度块：任务开始多久后出现；两次落盘之间至少间隔多久（省卡片调用）。
# 活动面板出现时机：**第一个工具事件立刻出**（见 _push_blocks 的 want_progress），
# 这里的 3 秒只兜底「没有任何工具、只有模型在思考」的情形——那种情况没内容可写，
# 提前铺一行「正在思考」纯属噪声。以前一刀切等 3 秒，结果最先跑的几步工具全被
# 吞掉，用户看到的永远是「正在思考」。
CARD_PROGRESS_AFTER = _env_float("DINGTALK_CARD_PROGRESS_AFTER", 3.0)
# 单个工具跑多久之后，在活动行上挂它自己的计时（秒）。低于这个值不写：短步骤
# 写计时纯属噪声，而且会和底部总耗时打架（两个钟对不上 = 看着像坏了）。
CARD_STEP_CLOCK_AFTER = _env_float("DINGTALK_CARD_STEP_CLOCK_AFTER", 5.0)
# 思考内容最多显示多少字（取最新一句，超长截尾）。0 = 不显示思考内容。
CARD_THOUGHT_MAX = _env_int("DINGTALK_CARD_THOUGHT", 60)
# 进度块刷新间隔。原来是 30s——真机反馈：块里那行「处理任务中，耗时 10 秒」会
# **冻住半分钟**，和底部每秒跳的状态行对不上，看着像卡死。Nielsen 的 10 秒阈值
# 说明超过 10s 用户就会怀疑是不是死了，所以这里收到 5s（远小于 10s，又不至于
# 变成高频调用；进度块只在步骤真正变化时才推，调用量本来就很低）。
CARD_PROGRESS_REFRESH = _env_float("DINGTALK_CARD_PROGRESS_REFRESH", 3.0)
# 「最近几步」面板最多几行（不含最上面那行「正在干什么」）。0 = 关闭。
CARD_TRAIL_MAX = _env_int("DINGTALK_CARD_TRAIL", 3)
# 过程行是否带具体目标名（文件名/程序名/关键词）。关掉就退回「读取文件」这类
# 泛化说法——卡片开了转发，文件名也算一点信息，不想露就设 0。
CARD_ACTION_DETAIL = _env_flag("DINGTALK_CARD_ACTION_DETAIL", True)
# 事件流静默多久后，状态行改说「已静默 Ns」。模型长思考时确实会几十秒没有
# 任何事件；把沉默如实标出来，好过让「正在执行」一直挂着假装在干活。
CARD_IDLE_AFTER = _env_float("DINGTALK_CARD_IDLE_AFTER", 30.0)
# 纯思考（没有工具在跑）时的静默阈值，比工具态短一半：工具跑 30 秒是**正常**的，
# 模型只是「在想」30 秒就让人开始怀疑了。两档分开，沉默提示才可信。
CARD_IDLE_THINKING = _env_float("DINGTALK_CARD_IDLE_THINKING", 12.0)
# 生成中要不要把工具链/备注铺成独立卡片块。默认 0（精简）——见 CardWriterV2._blocks。
# 想回退看老样式就设 DINGTALK_CARD_STREAM_TOOL_BLOCKS=1。
CARD_STREAM_TOOL_BLOCKS = _env_flag("DINGTALK_CARD_STREAM_TOOL_BLOCKS", False)
# 状态行（statusLine）刷新节流：它是独立一次 card/instances 调用，别变成每秒一次。
# 原为 3.0s——耗时数字就会 3 秒跳一次，看着不连续；降到 1.0s 后接近逐秒跳。
# 代价：状态行调用量从 0.33/s 涨到 1/s。整轮合计（正文 2/s + 状态 1/s）仍
# 远低于 20 QPS 的令牌桶上限，不会踩钉钉限流。想回退设 3。
CARD_STATUS_REFRESH = _env_float("DINGTALK_CARD_STATUS_REFRESH", 1.0)

# 正文正在推流时，**不要**同时每秒去改状态行 / blockList。
# 那是另一条接口（card/instances）的一次完整重渲染；参考实现
# （openclaw-channel-dingtalk）在提交 blockList 之前会**先把流式内容清掉**再写，
# 说明「流式正文」和「实例变量更新」在客户端是互相干扰的：一边在打字，
# 一边整卡重绘，打字就白打了。
# 真机症状正好对上：正文在推、状态行也在刷 → 正文看着一动不动，收尾时整段蹦出来。
# 所以只要最近 CARD_STREAM_QUIET 秒内推过正文，就把状态行/blockList 更新让路。
CARD_STREAM_QUIET = _env_float("DINGTALK_CARD_STREAM_QUIET", 2.0)

# 正文**此刻**没有积压、且距上一帧超过这么久，才允许动 instances（状态行/面板）。
# v4 的规则是「本轮推过正文就永久不再碰 instances」——它保住了打字效果，代价是
# 模型去跑工具的那十几秒里卡片彻底冻住（用户看到的就是「正在思考（已完成 3 步）」
# 一直不动）。v5 已把推帧速率压到客户端画得动的档位，积压正常情况下恒为 0，
# 所以判据可以回到「此刻有没有积压」，而不是「整轮有没有推过正文」。
CARD_INSTANCES_QUIET = _env_float("DINGTALK_CARD_INSTANCES_QUIET", 2.5)

# 从状态行里认出「已静默 24s」，好让进度块跟着说同一件事（两处口径必须一致）。
_SILENT_RE = re.compile(r"静默\s*(\d+)\s*s")

# ---- 匀速出字节拍（smooth streaming / typewriter pacing）----
# 生成中正文只推给流式变量 content（卡片处于 flowStatus=2「输入中」）；终态一翻
# flowStatus=3，卡片改渲染 blockList，content 整块当场消失。
#
# 官方约束（AI 卡片流式更新）：绑 markdown 的流式变量必须 isFull=true（覆盖式），
# 客户端**只按这一帧收到的内容**渲染——一帧塞多少字，就一次性蹦出多少字。
# 所以「打字机效果」不是客户端自己逐字播，而是我们**按节奏小步推**出来的。
#
# 真机血泪（2026-10-19 用户两次报「突兀 / 突然全出来」）：
#   模型吐字是突发式的——日志实测 629 字只用了 4.2 秒，而卡片 0.5 秒才推一帧，
#   最后一帧一口气带上 167 字，看起来就是「前面匀速、后面整页糊上来」。
#   只靠「终态前收干」救不了：生成中最后那几帧本身就是大块。
#
# 业界通用做法（Vercel AI SDK 的 smoothStream、Upstash 的 typewriter hook、
# ai-markdown 的 smooth streaming controller）**把「到达节奏」与「显示节奏」
# 彻底解耦**：收到的全量正文只当缓冲，由独立节拍器按固定速率往前揭示。
#
# 2026-10-19 四次真机事故后的定案（v4）：**全程同一个速率，连收尾也不许加速**。
#
# 为什么 v3（固定 80 字/秒 + 收尾提速到 120-300）仍然失败——这次的日志终于
# 把数字对上了：
#   * 推一帧 PUT 实测 85-135ms，再叠加进程级 QPS 令牌桶的 50ms 间隔，
#     节拍线程真实只能跑 **约 5-7 帧/秒**；
#   * 而单帧被 CARD_PACE_MAX_CHARS 硬掐在 18 字以内，于是**真实显示吞吐
#     ≈ 18 × 6 ≈ 90-110 字/秒**，而且 .env 把速率配成了 200 字/秒，
#     这个目标根本不可能达到（配得越高、积压越大）；
#   * 模型吐字 70-170 字/秒 > 显示吞吐 → 积压只增不减，生成一结束
#     收尾那段就以 300 字/秒猛冲（实测 75 字只用 0.58 秒，而正文段
#     平均 ~50 字/秒）。**用户看到的「流一半、剩下整段糊上来」，
#     本质是速率从 50 字/秒突然跳到 130-300 字/秒的感官差**，
#     不是某一帧特别大。
#   * 还有一个更隐蔽的坑：收尾开始时先调 `set_status()`（走 instances 整卡
#     重绘），而流式正文和实例变量在客户端是互相干扰的（见 CARD_STREAM_QUIET
#     的注释）。打字打到一半整卡重绘 → 画面冻在旧的一版 → 终态 blockList
#     一翻，全文一下出现。这是「半截 + 突然全出」的另一半原因。
#
# v4 的做法：
#   1) 显示速率固定 CARD_PACE_RATE（默认 60 字/秒，约每秒 2 行），**收尾同速**，
#      绝不在末尾提速——速率变化本身就是「突然糊上来」的来源；
#   2) 每帧 ≤ CARD_PACE_MAX_CHARS（默认 10 字，半行以内），按实测帧间隔折算，
#      帧快则字少、帧慢则字多，**始终维持同一个字/秒**；
#   3) 切点吸附到行/句边界，视觉上是「一个字一个字往外走」；
#   4) 收尾只负责「把积压按同一速率放完」，预算按剩余量算，不设提速上限；
#   5) 生成期间与收尾期间**都不碰** instances（状态行只在终态那一次写）。
# 2026-10-19 第五次真机事故（v5）后的定案：**问题不在我们推得快慢一致，而在推得比客户端能画更快**。
#
# 前四版都在调「服务端推帧节奏」，可日志已经证明 v4 的推帧是完全均匀的：
#   26 帧 / 有效速率 67 字/秒 / 单帧 1-10 字 / 最大帧间隔 0.14s（170 字）
#   73 帧 / 有效速率 66 字/秒 / 单帧 4-10 字 / 最大帧间隔 0.12s（456 字）
# 服务端匀速了，用户看到的却仍然是「打到一半，剩下整段蹦出来」，而且**每次都是
# 大约一半**——这个「固定比例」是关键线索：
#   * 钉钉官方文档写明流式更新「在客户端会呈现一种打字机效果」，即客户端自己还有
#     一层逐字动画；当服务端 66 字/秒而客户端只画得动约 30 字/秒时，积压按固定
#     比例线性增长，收尾翻终态那一刻把积压一次性放出来 = 永远停在「一半」。
#     比例与文本长度无关，正好对上四次事故里一直是一半的现象。
#   * 另一种可能是客户端按固定频率合并刷新（约 1-4 次/秒）。这种情况下一帧内
#     出现多少字就蹦多少字，所以同样要求「每秒少推一点」。
# 两种机制下解法一致：**把推送速率降到客户端肯定画得动的档位，并降低帧频**。
#   22 字/秒 + 0.25 秒/帧：每帧 5-6 字、每秒 4 帧——1Hz 合并刷新的客户端一次
#   只蹦半行，逐字动画的客户端每帧动画 0.18s < 0.25s 帧距，永远不积压。
CARD_PACE_TICK = _env_float("DINGTALK_CARD_PACE_TICK", 0.25)        # 节拍**间隔**（秒）
CARD_PACE_RATE = _env_float("DINGTALK_CARD_PACE_RATE", 22.0)         # 固定揭示速率（字/秒）
CARD_PACE_RATE_DRAIN = _env_float("DINGTALK_CARD_PACE_DRAIN_RATE", 22.0)  # 收尾速率（= 正常速率）
# 单帧字数上限：手机一行约 22-26 字。**任何**时候（含收尾）一帧都不许超过它，
# 否则就是「一次蹦出半行以上」。这是硬约束，优先级高于速率。
CARD_PACE_MAX_CHARS = _env_int("DINGTALK_CARD_PACE_MAX_CHARS", 8)
CARD_PACE_SNAP = _env_int("DINGTALK_CARD_PACE_SNAP", 8)              # 行/句边界回退窗口（字）
# ---- 流式「尾部窗口」（smooth streaming 的可见区）----
# 2026-10-19 真机反馈：长回答生成中，卡片里的「展开」按钮一直闪、点不开，
# 只有收尾翻完成态才点得动。
# 原因：流式接口只能 isFull=true **整块覆盖** `content`，每推一帧客户端就重绘
# 一次 markdown 容器，折叠状态跟着被重置 —— 4 帧/秒的重绘频率下，按钮永远
# 处于「刚画出来就被下一帧抹掉」的状态，点击事件自然接不住。
# 解法：生成中**只把最近一小段**塞进 content，让内容压根不超过客户端的折叠
# 阈值（不出现按钮，也就不存在闪烁）；全量正文在收尾那一刻随 blockList
# 一次性渲染，那次只画一帧，展开/收起稳定可用。
# 不影响打字机节奏：揭示位置、帧统计、收尾估算全部仍按**全量文本**记账，
# 只有真正发给接口的那一版是窗口切片（见 CardWriterV2._tail_window）。
# 0 = 关闭（回退到「全量塞进 content」的旧行为）。
CARD_TAIL_WINDOW = _env_int("DINGTALK_CARD_TAIL_WINDOW", 200)
# 群聊卡片头部的用户原话引用。它和正文一起算进「输入中」卡片的实际高度，
# 是触发客户端折叠（展开按钮）的因素之一；过线时优先砍它。
# 0 = 不引用（单聊本来就不引用，见 _quote_text）。
CARD_QUOTE = _env_int("DINGTALK_CARD_QUOTE", 1)
# 收尾最多等多久（秒）。按同速率放完是 remain / rate，再留一点余量；
# 超过这个上限才走兜底补推（兜底也按同速率，不整段灌）。
CARD_PACE_DRAIN_MAX = _env_float("DINGTALK_CARD_PACE_DRAIN_MAX", 120.0)
CARD_PACE_DRAIN_BUDGET = _env_float("DINGTALK_CARD_PACE_DRAIN_BUDGET", 10.0)
# 兜底分片：只有节拍线程异常 / 等待超时才走到这里。
CARD_DRAIN_CHUNK = _env_int("DINGTALK_CARD_DRAIN_CHUNK", 8)          # 每片最少几字
CARD_DRAIN_INTERVAL = _env_float("DINGTALK_CARD_DRAIN_INTERVAL", 0.1)  # 片间隔（秒）
# 翻终态前的保底等待：节拍器已保证最后一片很小，只需留一拍让客户端画完，
# 不再像旧实现那样冻 2-3 秒（用户会以为卡死）。
# v5：给到 1 秒——如果客户端那层逐字动画还欠着最后一帧，这一秒正好让它画完，
# 否则会在翻终态的一瞬间补出来（又是一次「突然出现」）。
CARD_SETTLE_BASE = _env_float("DINGTALK_CARD_SETTLE", 1.0)          # 保底等待
CARD_SETTLE_PER_CHAR = _env_float("DINGTALK_CARD_SETTLE_PER_CHAR", 0.02)  # 每字
CARD_SETTLE_MAX = _env_float("DINGTALK_CARD_SETTLE_MAX", 2.5)       # 上限


def _common_prefix_len(a, b):
    """两个字符串的公共前缀长度（用来估算「还有多少字没显示出来」）。"""
    n = min(len(a or ""), len(b or ""))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


class CardWriterV2:
    """基于公共模板的 AI 卡片写入器。

    与 CardWriter **保持同样的方法签名**，这样 execute() / _run_turn 可以无感替换：
        start() / stream() / footer() / note() / footer_text() / finish()
    额外能力（v1 做不到的）：
      * blockList 结构化分层：正文 / 进度 / 图片各自成块；
      * 长任务单行进度块（「执行检查中，耗时 1 分 20 秒」），生成中**只有这一块**
        （工具链铺满卡片太丑，真机反馈后砍成一行概览，细节留在日志）；
      * 底部单行状态行（在干嘛 | 模型 | 耗时）；
      * 卡片头部引用用户原话（**仅群聊**，单聊里原话就在卡片上方，不重复占版面）；
      * 超长回答按码点分片，而不是截断丢内容；
      * 失败时给出明确的重试提示。
    """

    # 让调用方知道正文**不需要**降级成 v1 的 markdown 子集
    passthrough_markdown = True

    def __init__(self, handler, msg, title="OpenCode",
                 min_interval=CARD_MIN_INTERVAL):
        self.handler = handler
        self.msg = msg
        self.title = title
        self.min_interval = min_interval
        self.notes = []
        self.truncated = False
        self.card = None                 # 外部只读 card.card_instance_id
        self._parts = []
        self._tool_kinds = []
        self._done = 0                    # 已完成的工具步数（真实计数，不编造）
        self._label = ""                  # 当前在跑的工具短标签
        self._action = ""                 # 当前在干什么（人话，见 card_v2.tool_action）
        self._trail = []                  # 最近几步（已完成，带耗时）
        self._silent = None               # 事件流静默秒数（超过阈值才写）
        self._thinking = False            # 模型在思考（没在跑工具）
        self._writing = False             # 正文正在打字（没在跑工具）
        self._thought = ""                 # 最新一句思考内容（reasoning part）
        self._step_secs = 0.0             # 当前工具已经跑了多久（长步骤才展示）
        self._blocks_cache = ""
        self._status = ""
        self._status_ts = 0.0
        self._t0 = time.time()
        self._last_stream_text = ""      # 最近一次**真正推上去**的正文
        self._prev_stream_text = ""      # 再上一次推的正文（估「最后一片新增了几字」）
        self._progress_ts = 0.0
        self._finished = False
        # 匀速出字节拍器状态（见 CARD_PACE_* 的注释）
        self._pace_lock = threading.Lock()
        self._pace_target = ""           # 目前**应该**显示到哪（调用方给的全量正文）
        self._pace_shown = ""            # 目前**已经**推上卡片的正文
        self._last_payload = ""          # 最近一次真正发出去的窗口切片（去重用）
        self._pace_stop = threading.Event()
        self._pace_thread = None
        self._pace_capped = False        # 正文撞到流式变量 10KB 上限
        self._pace_last_arrival = 0.0    # 最近一次正文变长的时间（仅用于日志/诊断）
        self._pace_drain = False         # 收尾阶段：略快一档，仍逐帧推
        self._pace_drain_rate = CARD_PACE_RATE_DRAIN  # 收尾速率（与正文同速，不加速）
        self._pace_step_ts = 0.0         # 上一次算每拍字数的时间（按真实 dt 折算速率）
        self._drain_frames = 0           # 收尾兜底补推的帧数（只用于统计）
        # 正文推流的观测数据（每帧耗时/字数）。用来回答「为什么看着像一次性蹦出来」：
        # 先确认服务端到底推了几帧、每帧多大、PUT 多久，再谈节拍怎么调。
        self._push_ts = 0.0              # 最近一次成功推帧的时间
        self._push_frames = 0            # 本轮成功推了多少帧
        self._push_ms = 0.0              # 推帧累计耗时（毫秒）
        # v4 诊断：真机上要能回答两个问题——
        #   ① 节拍器实际跑多快（帧间隔）② 实际显示了多少字/秒
        # 之前只记「配置的速率」，日志里写着 200 字/秒，实际只有 ~50，
        # 问题被这行假日志盖住了整整两轮。
        self._push_chars = 0             # 累计**新增**到卡片上的字数
        self._pace_t0 = 0.0              # 第一帧的时间（算有效速率用）
        self._last_push_ts = 0.0         # 上一帧成功时间（算帧间隔）
        self._max_gap = 0.0              # 最大帧间隔（秒）
        self._max_frame = 0             # 单帧最大新增字数
        self._min_frame = 9999           # 单帧最小新增字数

    # ---- 生命周期 ----
    def start(self) -> bool:
        try:
            card = card_v2.CardV2(self.msg, title=self.title)
            if not card.create(quote=self._quote_text(),
                               status=card_v2.status_line(extra="正在思考")):
                return False
        except Exception as e:  # noqa: BLE001
            logger.warning("创建 v2 卡片失败：%s", e)
            return False
        self.card = card
        register_card_owner(card.out_track_id, self.msg.conversation_id)
        self._pace_start()
        return True

    # ---- 匀速出字节拍器 ----
    def _pace_start(self):
        """起一个后台节拍线程：按固定节奏把缓冲里的正文一点点放出去。"""
        if self._pace_thread is not None and self._pace_thread.is_alive():
            return
        self._pace_stop.clear()
        t = threading.Thread(target=self._pace_loop, name="card-pace", daemon=True)
        self._pace_thread = t
        t.start()

    def _pace_loop(self):
        """节拍线程：`CARD_PACE_TICK` 是**帧间隔**，不是「推完再歇这么久」。

        每次推帧本身要花 PUT 时间（实测 83ms），旧实现是「推完再固定睡 100ms」，
        真实间隔变成 ~183ms，等于白丢 45% 的吞吐——模型 260 字/秒时必然掉队，
        掉队的那部分最后只能堆到收尾一次性喷出来。这里改成扣掉推帧耗时。
        """
        while not self._pace_stop.is_set():
            if self._finished:
                break
            t0 = time.time()
            try:
                self._pace_step()
            except Exception as e:  # noqa: BLE001
                logger.warning("流式出字节拍异常：%s", e)
            elapsed = time.time() - t0
            self._pace_stop.wait(max(0.01, CARD_PACE_TICK - elapsed))

    @staticmethod
    def _pace_cut(text, start, end):
        """把切点吸附到行/句边界，让画面「一行一行出来」而不是碎块糊上来。

        优先整行（下一个换行符落在允许的超额窗口内），其次回退到最近的
        行尾 / 句末标点，最后才硬切。中文窄屏一行约 18-22 字，按行切正好
        一帧一行。
        """
        n = len(text)
        end = min(n, max(start + 1, end))
        nl = text.find("\n", end, min(n, end + CARD_PACE_SNAP))
        if nl != -1:
            return nl + 1
        floor = start + max(1, (end - start) // 2)
        for i in range(min(end, n) - 1, floor - 1, -1):
            if text[i] in "\n。！？；：":
                return i + 1
        return end

    def _pace_step(self):
        """放一拍：**固定速率**揭示缓冲里的正文（真·打字机）。

        不再按「积压 / 距截止时间」算速率（旧 deadline law）。原因见上方
        CARD_PACE_* 的注释：模型吐字 70-170 字/秒，任何「追上生成」的策略都会
        变成「生成多快就糊多快」，最后剩下的积压只能在收尾一次性补推。

        现在无论积压多少、无论是不是收尾，一律按同一个速率匀速放
        （收尾用 _pace_drain_rate，默认与 CARD_PACE_RATE 相同）。
        """
        now = time.time()
        with self._pace_lock:
            target = self._pace_target
            shown = self._pace_shown
            drain = self._pace_drain
            drain_rate = self._pace_drain_rate
            last_step = self._pace_step_ts
            self._pace_step_ts = now
        start = _common_prefix_len(shown, target)
        remain = len(target) - start
        if remain <= 0:
            return
        # 每拍字数按**实测间隔**折算，而不是拿 TICK 死算：
        # 推一帧 PUT 实测 85-135ms，线程真实周期约 0.13-0.2s；若每拍固定
        # 「60 字/秒 × 0.05s = 3 字」，实际速率会掉到 20 字/秒以下。这里用真实
        # dt 折算，速率才能和 CARD_PACE_RATE 对上。dt 上限 3 拍：模型静默几秒后
        # 不许攒出一大段「信用」然后一次吐出来。
        dt = (now - last_step) if last_step else CARD_PACE_TICK
        dt = min(max(dt, CARD_PACE_TICK * 0.5), CARD_PACE_TICK * 3.0)
        cap = drain_rate if drain else CARD_PACE_RATE
        step = max(1, int(round(cap * dt)))
        step = min(step, CARD_PACE_MAX_CHARS)
        cut = self._pace_cut(target, start, start + step)
        # 吸附到行/句边界可能往后多吃十几个字（整行），但一帧最多一行多一点：
        # 超过上限就硬切，宁可切在句中断开，也不要一帧蹦两行。
        if cut - start > CARD_PACE_MAX_CHARS:
            cut = start + CARD_PACE_MAX_CHARS
        nxt = target[:cut]

        if len(nxt.encode("utf-8")) > card_v2.CARD_MAX_BYTES:
            # 流式变量本身有 10KB 上限（钉钉官方：单次 ≤1K、总量 ≤10K）。
            # 越过上限后内容再怎么推客户端也不会变，继续推只是白烧 QPS，
            # 所以这里推到上限就收手，剩下的交给终态 blockList 承载。
            # 开了尾部窗口就不必收手——发出去的那一版永远很小（见 _tail_window）。
            if CARD_TAIL_WINDOW <= 0:
                nxt = nxt.encode("utf-8")[:card_v2.CARD_MAX_BYTES - 64].decode(
                    "utf-8", "ignore")
                self._pace_capped = True
                logger.info("流式正文到 10KB 上限，后续内容改由终态卡片承载")
        if nxt == shown:
            return
        # 真正发给接口的只有尾部窗口；全量口径（_pace_shown 等）仍按 nxt 记账。
        payload = self._tail_window(nxt)
        if payload == self._last_payload:
            with self._pace_lock:
                self._pace_shown = nxt
            return
        t_push = time.time()
        try:
            ok = self.card.stream(payload, force=True, min_interval=0)
        except Exception as e:  # noqa: BLE001
            logger.warning("流式推帧异常：%s", e)
            ok = False
        if ok:
            self._push_ts = time.time()
            self._last_payload = payload
            self._push_frames += 1
            self._push_ms += (self._push_ts - t_push) * 1000.0
            # 诊断：这一帧**新增**了几个字、距上一帧隔了多久。
            # 「突然糊上来」永远等于「单帧新增字数量级跳变」，所以这里必须
            # 按帧记，而不是只看终稿总字数。
            added = len(nxt) - _common_prefix_len(shown, nxt)
            if added > 0:
                self._push_chars += added
                self._max_frame = max(self._max_frame, added)
                self._min_frame = min(self._min_frame, added)
                if not self._pace_t0:
                    self._pace_t0 = self._push_ts
                if self._last_push_ts:
                    self._max_gap = max(self._max_gap,
                                        self._push_ts - self._last_push_ts)
                self._last_push_ts = self._push_ts
            logger.debug("流式帧 #%d 正文 %d 字 / 窗口 %d 字 / 新增 %d 字 / "
                         "%d 字节 / PUT %.0fms",
                         self._push_frames, len(nxt), len(payload), added,
                         len(payload.encode("utf-8")), (self._push_ts - t_push) * 1000)
            with self._pace_lock:
                self._pace_shown = nxt
            # 只记「真正推上去的」正文（收尾估算最后一片画完要多久）
            self._prev_stream_text = self._last_stream_text
            self._last_stream_text = nxt

    @staticmethod
    def _tail_window(text):
        """生成中真正发给流式接口的那一版正文（默认只取尾部一段）。

        为什么要截：`content` 变量只能整块覆盖，长内容会被客户端折叠出
        「展开」按钮，而每帧覆盖都会重置折叠状态 —— 表现就是按钮一直闪、
        点不动（见 CARD_TAIL_WINDOW 的注释）。只推最后一小段，内容永远
        不超过折叠阈值，生成期间就不需要这个按钮了。

        记账口径**不受影响**：`_pace_shown` / `_last_stream_text` 等仍然
        保存全量文本，只有 payload 走这里。CARD_TAIL_WINDOW=0 时原样返回。
        """
        n = int(CARD_TAIL_WINDOW or 0)
        text = text or ""
        if n <= 0 or len(text) <= n:
            return text
        start = len(text) - n
        # 尽量从行首开始，免得一进来就是半行（看起来像被啃掉一块）。
        nl = text.find("\n", start, min(len(text), start + 40))
        if nl != -1:
            start = nl + 1
        return text[start:]

    def _quote_text(self):
        """卡片头部引用的用户原话——**只在群聊里引用**。

        单聊里用户原话就贴在卡片正上方，再抄一遍纯占版面（早期版本两边都引用，
        卡片顶部永远挂着一行重复的话，真机看着很蠢）。群聊里消息和卡片会被
        刷走，引用才有定位价值。

        DINGTALK_CARD_QUOTE=0 可整体关掉：引用会顶高「输入中」卡片，
        把折叠阈值顶到自己头上（真机：长回答 + 引用 + 活动面板 → 展开按钮闪）。
        """
        if not CARD_QUOTE or not self._is_group():
            return ""
        try:
            t = (self.msg.text.content or "").strip() if self.msg.text else ""
        except Exception:  # noqa: BLE001
            t = ""
        if not t:
            try:
                t = (getattr(self.msg, "get_text_list", lambda: None)() or "")
                t = t if isinstance(t, str) else ""
            except Exception:  # noqa: BLE001
                t = ""
        return t[:300]

    def _is_group(self):
        """判定群聊（判定规则与 card_v2 的 openSpace 保持一致）。"""
        ctype = str(getattr(self.msg, "conversation_type", "") or "")
        conv = str(getattr(self.msg, "conversation_id", "") or "")
        staff = str(getattr(self.msg, "sender_staff_id", "") or "")
        return ctype == "2" or bool(conv and staff and conv.endswith(staff))

    def stream(self, text, force=False):
        """把「当前应该显示的全量正文」交给节拍器，**不直接推卡片**。

        为什么要绕一层：模型吐字是突发式的（实测 629 字 4.2 秒，期间还夹着成块
        到达的网络分片），收到多少就推多少 → 用户看到「前几帧匀速、最后一帧糊
        一大段」。真正的推帧节奏由后台节拍线程 `_pace_loop` 控制，见 CARD_PACE_*。
        force 参数保留只为和 v1 写入器保持同一签名，这里不再区分——节拍器本来
        就每 0.1 秒滚动一次。
        """
        if not self.card or self._finished:
            return False
        text = text or ""
        with self._pace_lock:
            grew = len(text) > len(self._pace_target)
            self._pace_target = text
        if grew:
            # 只做诊断记录：v3 的揭示节奏是固定速率，与到达节奏无关。
            self._pace_last_arrival = time.time()
        self._pace_start()
        return True

    def footer(self, text, force=False):
        """把页脚文本落到状态行；同时按需刷新 blockList（工具链 / 进度）。

        状态行是**独立的卡片接口调用**（card/instances），而它带耗时、每秒都
        在变——不限流就会变成「每 0.8 秒一次 PUT」，很容易撞 QPS 限流。这里
        按 CARD_STATUS_REFRESH 节流；实时正文本身由 stream() 走流式接口负责。
        """
        if not self.card or self._finished:
            return
        now = time.time()
        # v5：判据从「本轮推过正文就永久让路」改成「**此刻**还有积压 / 刚推过帧」。
        # 原因：v4 那条规则虽然保住了打字，但也把活动面板锁死了——模型跑去跑工具
        # 的那十几秒正好是用户最想知道「在干什么」的时候，卡片却一动不动。
        # 只在这两种情况下让路，仍然避免「一边打字一边整卡重绘」：
        #   ① 节拍器还没把正文推完（还有积压）；
        #   ② 距上一帧不到 CARD_INSTANCES_QUIET 秒（客户端可能还在画）。
        with self._pace_lock:
            pending = bool(self._pace_shown) and self._pace_shown != self._pace_target
        quiet = (now - self._push_ts) if self._push_ts else 1e9
        if (not force) and (pending or quiet < CARD_INSTANCES_QUIET):
            if text:
                self._status = text
            return
        if text and text != self._status:
            self._status = text
        pushed = self._push_blocks(force=force)
        # 面板没变化（或还没到出现时机）时，状态行单独刷一次——否则耗时数字会
        # 冻在上一帧，「看着像卡死」正是这么来的。有 blocks 一起推的时候，
        # set_blocks 已经把 statusLine 带上了，不必再多一次整卡重绘。
        if (not pushed) and self._status and (
                force or now - self._status_ts >= CARD_STATUS_REFRESH):
            self._status_ts = now
            self.card.set_status(self._status)

    def footer_text(self, parts, elapsed=None, model=None, status=None):
        """v2 的状态行要求**单行**（多行在钉钉客户端只会刷新尾行）。

        生成中只保留两段：`当前在干嘛` + `模型 | 耗时`。
        工具链不再堆进卡片（见 _blocks 的说明），备注只在顶掉状态时出现——
        否则「🔧 正在执行 | 🔓 自动放行 bash | 模型名 | 12 秒」四段挤在一行，
        比正文还抢眼（真机反馈：生成中的卡片比 v1 还丑）。

        2026-10-19：这里从「按工具种类猜」改成「读 tool part 的 input/time」。
        种类只有 7 个，卡片上永远是「正在执行 / 正在检查文件」这种没信息量的话；
        而 OpenCode 的 part 里带着 name / state.input / time，能拼出
        「读取 bridge.py · 1.2s」。安全边界见 card_v2.tool_action。
        """
        self._parts = parts or []
        self._tool_kinds = []
        done = 0
        running = None
        trail = []
        for p in self._parts:
            if not (isinstance(p, dict) and p.get("type") == "tool"):
                continue
            stt = card_v2.tool_state(p)
            if stt == "running":
                running = p                    # 只认**最后一个**在跑的
            elif stt in ("completed", "error", "failed"):
                done += 1
                trail.append(p)
            k = card_v2.tool_kind(p.get("name") or p.get("tool") or "")
            if k not in self._tool_kinds:
                self._tool_kinds.append(k)
        self._done = done
        if CARD_ACTION_DETAIL:
            self._action = card_v2.tool_action(running) if running else ""
        else:
            self._action = (card_v2.tool_label(running.get("name") or "")
                            if running else "")
        self._label = self._action or (card_v2.tool_action(trail[-1])
                                       if trail else "")
        self._trail = trail[-CARD_TRAIL_MAX:] if CARD_TRAIL_MAX > 0 else []
        # 单个工具跑得久时，把计时挂到活动行上（见 _blocks）。这里只算一次，
        # 保证同一次刷新里活动行和状态行读到的是同一瞬间。
        self._step_secs = card_v2.tool_secs(running) if running else 0.0
        self._thought = latest_thought(self._parts)
        # 状态行说「在思考」时，进度块就不要硬套工具种类（会把几轮之前的
        # 「执行检查」当成现在在做的事，属于假进度）。
        self._thinking = (not running) and any(
            k in (status or "") for k in ("思考", "静默", "等待"))
        # 正文正在往卡片上打字时，别再说「正在处理」——那句话在真机上看着
        # 就像「没动静」。和状态行口径保持一致：✍ 正在写回答。
        self._writing = (not running) and any(
            k in (status or "") for k in ("写回答", "输出", "作答"))
        m = _SILENT_RE.search(status or "")
        self._silent = int(m.group(1)) if m else None
        head = card_v2.strip_emoji_head(status or "")
        if self._action:
            head = self._action
        if self.notes:
            note = card_v2.strip_emoji_head(self.notes[-1])
            if note:
                head = note if not head else f"{head} | {note}"
        # 长步骤的计时已经挂在活动行上了 —— 状态行再写一次总耗时就是两个钟，
        # 真机上两个数字对不上比不显示还糟。这里让位，全卡只留一个「钟」。
        tail = card_v2.status_line(
            model=model,
            elapsed=None if self._step_secs >= CARD_STEP_CLOCK_AFTER else elapsed)
        if head and tail:
            return f"{head} | {tail}"
        return head or tail

    def note(self, text: str):
        if text and text not in self.notes:
            self.notes.append(text)
            del self.notes[:-3]

    def hint_action(self, label: str):
        """事件流先到、REST 明细还没到时的**临时**动作名。

        事件 `session.tool.input.started` 只带工具名（没有 input/time），而
        「执行命令 · 读取 bridge.py」这种明细要等下一次 REST 同步（最长 3 秒）。
        没有这个提示，面板要等明细到齐才出现，第一步工具就隐身在「正在思考」里。
        明细一到就会被 footer_text 覆盖，所以这里只是占位。
        """
        if self._finished or not label or self._action:
            return
        self._action = label

    # ---- block 组装 ----
    def _blocks(self):
        """把当前状态折算成 blockList。

        生成中**只有「活动面板」**：第一行是「正在干什么」，下面最多
        CARD_TRAIL_MAX 行最近步骤（`✓ 读取 bridge.py · 1.2s`）。
        不铺完整工具链、不铺备注——七八行灰色引用条糊在正文上面比什么都难看
        （真机反馈）。参考实现（openclaw dingtalk）默认也是
        `cardStreamingMode: "answer"`：流式期间只刷正文，细节留到终态再合并。

        为什么一行一个 block：同一个 block 里的多行 markdown，钉钉只刷新尾行，
        步数会看着停在 0。所以面板由多个**单行 block** 拼成。

        终态化时整块移除（见 finish 的 finalize 调用）。
        """
        blocks = []
        el = time.time() - self._t0
        # 有真实活动（工具在跑 / 已跑过几步）就**立刻**出面板，不再等 3 秒：
        # 最先跑的几步工具往往发生在头 3 秒里，等满 3 秒再画等于把那几步吞了。
        # 只有「纯思考、没有任何工具」时才等，避免开局就铺一行没信息量的字。
        want = (el >= CARD_PROGRESS_AFTER or bool(self._action)
                or bool(self._trail))
        if want and not self._finished:
            # 刻意**不再把耗时写进进度块**（长步骤除外，见 step_secs）：
            # 底部状态行每秒都在跳秒数，块里再写一个就会出现两个对不上的时间。
            blocks.append(card_v2.block_activity(
                card_v2.activity_header(
                    action=self._action, done=self._done,
                    thinking=self._thinking, silent=self._silent,
                    writing=self._writing,
                    step_secs=(self._step_secs
                               if self._action and
                               self._step_secs >= CARD_STEP_CLOCK_AFTER else None))))
            # 思考内容单独一行（同一个 block 里的多行只会刷新尾行，见上）。
            if self._thought and self._thinking:
                tb = card_v2.block_thought(self._thought, CARD_THOUGHT_MAX)
                if tb:
                    blocks.append(tb)
        if CARD_TRAIL_MAX > 0:
            # 最近的在最上面（紧贴「正在干什么」那行），往下依次更早——
            # 视线从上到下就是「现在 → 刚才 → 更早」，比正序更好扫。
            for p in reversed(self._trail):
                b = card_v2.block_activity(
                    card_v2.activity_step(p, detail=CARD_ACTION_DETAIL),
                    plain=card_v2.trail_line(p), tool=True)
                if b:
                    blocks.append(b)
        if CARD_STREAM_TOOL_BLOCKS:            # 老样式（开关回退用）
            for k in self._tool_kinds:
                blocks.append(card_v2.block_tool(f"{k}"))
            for n in self.notes:
                blocks.append(card_v2.block_tool(n))
        return [b for b in blocks if b]

    # ---- 结构化提问（表单卡：选项点选 + 提交）----
    def ask(self, questions, timeout=None, qid=None):
        """把结构化问题渲染成**提问卡**（表单模板），等用户点选提交。

        返回 (action, params)（params 里带 form 表单答案）；超时返回 None
        （调用方回退文字提问）。**未打开 ASK_CARD 时直接返回 None**——不是所有
        情况都能靠卡片问，关着时走老的「代答 + 提示改用文字」，绝不干等。

        questions: [{"id","title","options":[{"label","value"}],"description"}]
        """
        if not (self.card and not self._finished and card_v2.ASK_CARD_ENABLED):
            return None
        fields = []
        for i, q in enumerate(questions or []):
            name = str(q.get("id") or f"q{i}")
            f = card_v2.ask_field(name, q.get("title") or name, q.get("options") or [])
            if not f:
                return None                # 没有选项（自由文本域）→ 回退文字提问
            fields.append(f)
        if not fields:
            return None
        timeout = (timeout if timeout is not None
                   else card_v2.ASK_CARD_TIMEOUT)
        conv = str(getattr(self.msg, "conversation_id", "") or "")
        qid = qid or ("ask_" + uuid.uuid4().hex[:12])
        ask_card = card_v2.AskCard(self.msg, qid,
                                   title=questions[0].get("title") or "请选择",
                                   desc=questions[0].get("description") or "",
                                   fields=fields)
        if not ask_card.create():
            logger.warning("提问卡投放失败，回退文字提问")
            return None
        register_card_owner(ask_card.out_track_id, conv)
        CARD_ACTIONS.arm(conv)
        logger.info("提问卡已投放 qid=%s outTrackId=%s（%.0fs 内等用户点选）",
                    qid, ask_card.out_track_id, timeout)
        action, params = CARD_ACTIONS.wait(conv, timeout)
        answers = card_v2.parse_ask_answer(params) if params else None
        if not action:
            logger.info("提问卡超时未回答（%.0fs）", timeout)
            ask_card.finish(card_v2.ASK_STATUS_EXPIRED)
            return None
        # 作答完必须把卡片切到终态，否则表单一直挂在「待回答」态（看着像没生效）
        if params.get("user_cancel"):
            ask_card.finish(card_v2.ASK_STATUS_CANCELLED)
        else:
            ask_card.finish(card_v2.ASK_STATUS_SUBMITTED,
                            card_v2.format_ask_summary(fields, answers))
        return action, params

    def _push_blocks(self, force=False):
        """把 blockList 推到卡片上（带去重 + 进度刷新节流）。

        返回 True 表示这次**真的**发了一次 set_blocks（里面已带 statusLine），
        调用方据此决定要不要再单独刷一次状态行——避免一次 footer 里两次整卡重绘。
        """
        if not self.card or self._finished:
            return False
        now = time.time()
        el = now - self._t0
        want_progress = (el >= CARD_PROGRESS_AFTER or bool(self._action)
                         or bool(self._trail))
        if not force:
            if not want_progress:
                return False
            if now - self._progress_ts < CARD_PROGRESS_REFRESH:
                return False
        self._progress_ts = now
        blocks = self._blocks()
        sig = card_v2.blocks_to_json(blocks)
        if sig == self._blocks_cache:
            return False
        self._blocks_cache = sig
        self.card.set_blocks(blocks, status=self._status)
        return True

    # ---- 图片 ----
    def append_image(self, path, caption=""):
        """把本地图片上传成 mediaId 并挂成 image 块（替代另发一条图片消息）。"""
        if not self.card or self._finished:
            return False
        media_id = self.card.upload_image(path)
        if not media_id:
            return False
        try:
            self._image_blocks = getattr(self, "_image_blocks", [])
            self._image_blocks.append(card_v2.block_image(media_id, caption))
        except Exception:  # noqa: BLE001
            return False
        self._push_blocks(force=True)
        return True

    def _collect_images(self, text):
        """从回答里找出本地图片路径，上传成 image 块，并把 markdown 图片语法摘掉。

        支持两种写法：
          ![说明](C:\\path\\to\\chart.png)
          [IMAGE: C:\\path\\to\\chart.png]
        外链图片保持原样（钉钉卡片支持 markdown 外链图片）。
        """
        if not text or not self.card:
            return text
        found = []
        pat = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
        for m in pat.finditer(text):
            raw = m.group(2).strip()
            if re.match(r"^[a-zA-Z]:[\\/]", raw) or raw.startswith("file:///"):
                p = raw.replace("file:///", "").replace("/", os.sep)
                if os.path.exists(p):
                    found.append((p, m.group(1)))
        for m in re.finditer(r"\[IMAGE:\s*([^\]]+)\]", text):
            p = m.group(1).strip().replace("/", os.sep)
            if os.path.exists(p):
                found.append((p, ""))
        if not found:
            return text
        out = text
        for p, cap in found:
            out = out.replace(f"![{cap}]({p})", "").replace(f"[IMAGE: {p}]", "")
        self._image_blocks = getattr(self, "_image_blocks", [])
        for p, cap in found[:4]:
            mid = self.card.upload_image(p)
            if mid:
                self._image_blocks.append(card_v2.block_image(mid, cap))
                logger.info("已把本地图片挂到 v2 卡片：%s", os.path.basename(p))
        return out.strip()

    # ---- 终态 ----
    def _settle_seconds(self):
        """估算客户端把**最后一片**正文画完还要多久。

        `_last_stream_text` / `_prev_stream_text` 是最近两次真正生效的推送；
        两者公共前缀之后的长度 =「最后一帧新出现了几个字」。
        注意不能用「还剩多少字没推」来估（那是旧实现，见 _drain_stream）。
        """
        streamed = self._last_stream_text or ""
        prev = self._prev_stream_text or ""
        tail = max(0, len(streamed) - _common_prefix_len(prev, streamed))
        return min(CARD_SETTLE_MAX,
                   CARD_SETTLE_BASE + tail * CARD_SETTLE_PER_CHAR)

    def _drain_stream(self, text, status=""):
        """终态化前把节拍器的积压吐干净，并且不再冻好几秒。

        推帧统一交给后台节拍线程（见 CARD_PACE_*），这里只做三件事：
        1) 等节拍器把 `_pace_target` 追平（上限 CARD_PACE_DRAIN_MAX）；
        2) 万一线程异常 / 超时，自己按同样的小步兜底补齐；
        3) 留一拍让客户端画完最后一片——节拍器保证最后一片很小，所以这段
           等待从旧实现的 2-3 秒降到 0.3 秒上下。
        """
        if not self.card:
            return
        # v4：**不在收尾开始时改状态行**。
        # 状态行走 `/card/instances`，是一次整卡重绘；而卡片此时还处在
        # flowStatus=2 的流式态。参考实现与两次真机事故都证明：一边流式推正文、
        # 一边整卡重绘，客户端会把打字内容冻在旧的一版，然后等终态 blockList
        # 一翻，全文「啪」一下出现——正是用户报的「流一半、剩下全出来」。
        # 终态状态行跟着 finalize 一起写（finish() 已把 status 传下去），
        # 少这一次 instances 调用，流式那段就不会被打断。
        with self._pace_lock:
            self._pace_target = text
            self._pace_drain = True
        # 收尾速率 = 正常速率（**不加速**）。原来按「剩余量 / 预算」提速到
        # 300 字/秒，用户看到的就是「前面慢慢打、最后整段糊上来」。
        with self._pace_lock:
            shown = self._pace_shown
        remain_now = max(0, len(text) - _common_prefix_len(shown, text))
        drain_rate = CARD_PACE_RATE_DRAIN if CARD_PACE_RATE_DRAIN > 0 else CARD_PACE_RATE
        with self._pace_lock:
            self._pace_drain_rate = drain_rate
        budget = min(
            CARD_PACE_DRAIN_MAX,
            remain_now / max(1.0, drain_rate) + CARD_PACE_DRAIN_BUDGET)
        logger.info("收尾打字：剩 %d 字，按 %.0f 字/秒（与正文同速）逐帧推，"
                    "最长等 %.1fs", remain_now, drain_rate, budget)
        deadline = time.time() + budget
        while time.time() < deadline and not self._finished:
            if self._pace_capped:
                break
            with self._pace_lock:
                shown = self._pace_shown
            if _common_prefix_len(shown, text) >= len(text):
                break
            time.sleep(0.02)

        with self._pace_lock:
            shown = self._pace_shown
        start = _common_prefix_len(shown, text)
        remain = len(text) - start
        if remain > 0 and not self._pace_capped:
            logger.warning("节拍器未追平（差 %d 字），兜底按同速率补推", remain)
            # 兜底也**不许整段灌**：按 drain_rate 折算出每片字数，一片一片推。
            # 注意不要再用 CARD_DRAIN_CHUNK=8 当下限——那会把兜底速率抬到
            # 80 字/秒以上，和正文段对不上，末尾又会「快一截」。
            chunk = int(round(drain_rate * CARD_DRAIN_INTERVAL))
            chunk = max(1, min(chunk, CARD_PACE_MAX_CHARS))
            pos = start
            while pos < len(text) and not self._finished:
                nxt = self._pace_cut(text, pos, pos + chunk)
                nxt = min(nxt, pos + CARD_PACE_MAX_CHARS)
                pos = nxt

                try:
                    self.card.stream(self._tail_window(text[:pos]),
                                     force=True, min_interval=0)
                    self._drain_frames += 1
                except Exception as e:  # noqa: BLE001
                    logger.warning("终态前补推正文失败：%s", e)
                    break
                with self._pace_lock:
                    self._pace_shown = text[:pos]
                time.sleep(CARD_DRAIN_INTERVAL)

        wait = self._settle_seconds()
        if wait > 0:
            logger.info("终态前等最后一片画完：%.2fs（终稿 %d 字）", wait, len(text))
            time.sleep(wait)

    def finish(self, text, footer="", failed=False, parts=None) -> bool:
        if not self.card or self._finished:
            return False
        if parts:
            self._parts = parts
        text = self._collect_images(text or "")
        if failed:
            text = (text or "（出错了，没有拿到回答）") + \
                "\n\n> 直接重发上一条消息即可重试。"
        status = footer or self._status
        # 终态留一行过程摘要：活动面板会被整块移除，否则事后完全看不到走过
        # 哪几步、哪一步失败（见 process_summary）。
        if any(isinstance(p, dict) and p.get("type") == "tool"
               for p in (self._parts or [])) and "个工具调用" not in (status or ""):
            summary = process_summary(self._parts,
                                      elapsed=time.time() - self._t0,
                                      failed=failed)
            status = f"{status} · {summary}" if status else summary
        # 先收干流式正文（并锁死状态行），再翻终态——顺序不能反，
        # 翻转后 content 就不再渲染，补推也没用了。
        self._drain_stream(text, status)
        # 统计放在收尾**之后**：收尾那几帧也是真实推上去的，漏掉就会得出
        # 「11 帧推完 502 字」这种自相矛盾的日志（旧实现正是这样把问题藏住了）。
        total_frames = self._push_frames + self._drain_frames
        if total_frames:
            # 有效速率 = 实际新增字数 / 首帧到末帧的时间。**这才是真相**：
            # 以前这里打印的是配置值（200 字/秒），实际跑 50 字/秒，
            # 日志自己把自己骗了两轮。
            span = max(0.001, (self._last_push_ts - self._pace_t0)
                       if self._pace_t0 and self._last_push_ts else 0.001)
            eff = self._push_chars / span
            logger.info(
                "正文推流统计：%d 帧（收尾 %d 帧），平均 PUT %.0fms，"
                "有效速率 %.0f 字/秒（目标 %.0f），单帧新增 %d-%d 字，"
                "最大帧间隔 %.2fs，终稿 %d 字",
                total_frames, self._drain_frames,
                self._push_ms / max(1, self._push_frames or 1),
                eff, CARD_PACE_RATE,
                self._min_frame if self._min_frame != 9999 else 0,
                self._max_frame, self._max_gap, len(text or ""))
        self._pace_stop.set()
        self._finished = True
        # 终态卡片：思考/工具/进度都收掉，只留正文 + 图片。
        # 注意：该模板的 blockList **没有按钮块**，所以失败态做不了「重试」按钮
        # （早期版本塞了 type=4，模板不渲染）。这里只保证失败信息说清楚。
        blocks = list(getattr(self, "_image_blocks", []) or [])
        return self.card.finalize(
            content=text, blocks=blocks, failed=failed,
            status=status or card_v2.status_line(
                extra="❌ 执行失败" if failed else "✅ 完成"))


def make_writer(handler, msg, title="OpenCode"):
    """按开关选卡片写入器：v2 优先，不可用则回退 SDK 卡片（v1）。"""
    if CARD_V2_OK:
        w = CardWriterV2(handler, msg, title)
        if w.start():
            return w
        logger.warning("v2 卡片不可用，回退 SDK 卡片")
    w = CardWriter(handler, msg, title)
    return w if w.start() else None


# ---------------------------------------------------------------- 挂起项兜底
def _ask_via_card(writer, fields, timeout=None):
    """把 question 表单渲染成结构化提问卡（选项点选），等用户提交。

    返回可直接交给 reply_form 的答案 dict；**任何拿不到答案的情况都返回 None**，
    让调用方回退到「代答 + 提示改用文字」的老路（宁可退回老行为，也不能卡住整轮）。

    只处理**有选项**的字段：需要自由文本输入的字段手机端点不了，整体回退。
    整张表单一次投一张卡（多问题时域并列），用户选完点「提交」。
    """
    if not fields:
        return None
    if not (CARD_V2_OK and writer is not None
            and isinstance(writer, CardWriterV2)):
        return None
    if not getattr(card_v2, "ASK_CARD_ENABLED", False):
        return None                      # 提问卡未打开 -> 走老路径，不干等

    questions = []
    keys = []
    for i, f in enumerate(fields):
        key = f.get("key") or f"q{i}"
        opts = [{"label": o.get("label") or o.get("value"),
                 "value": o.get("value") or o.get("label")}
                for o in (f.get("options") or []) if isinstance(o, dict)]
        if not opts:
            return None                  # 含自由文本字段 → 回退
        title = (f.get("title") or f.get("label") or f.get("question")
                 or f.get("description") or "请选择")
        questions.append({"id": key, "title": title, "options": opts,
                          "description": f.get("description") or ""})
        keys.append(key)

    res = writer.ask(questions, timeout=timeout)
    if not res:
        return None                      # 投放失败 / 超时 → 回退
    action, params = res
    if not action:
        return None
    answers = card_v2.parse_ask_answer(params)
    if not answers:
        logger.info("提问卡回调里没有表单答案（action=%s），回退文字提问", action)
        return None
    picked = {}
    for key in keys:
        if key in answers:
            picked[key] = answers[key]
    if not picked:
        return None
    return picked


def handle_pending_gates(oc: OpenCodeService, session_id: str, writer=None) -> int:
    """主动应答「等客户端回话」的挂起项：权限请求 + question 表单。

    手机钉钉没有应答 UI：只要有一条挂起，整轮就会干等到 wait_idle 超时
    （原来 900s）。所以这里按策略自动应答：
      * 低风险动作（读文件、搜索等）→ once 放行；
      * 其余 → reject，并让 agent 用文字向用户说明后再试；
      * question 表单 → 回一句「不支持交互式提问」，让 agent 改用纯文本。
    返回处理条数。
    """
    handled = 0

    try:
        for req in oc.permissions(session_id):
            action = str(req.get("action") or "").lower()
            rid = req.get("id")
            if not rid:
                continue
            res = "、".join(map(str, req.get("resources") or []))[:80]
            if action in AUTO_ALLOW_PERMISSIONS:
                oc.reply_permission(session_id, rid, "once",
                                    message="（钉钉机器人自动放行低风险权限）")
                logger.info("自动放行权限：%s %s", action, res)
                if writer:
                    writer.note(f"🔓 自动放行 {action}")
            else:
                oc.reply_permission(session_id, rid, "reject", message=(
                    "用户在手机钉钉上没有权限确认界面，无法批准该操作。"
                    "请用纯文本向用户说明你要做什么、为什么需要它，"
                    "等用户明确同意后再执行。"))
                logger.warning("自动拒绝权限：%s %s", action, res)
                if writer:
                    writer.note(f"⛔ 自动拒绝 {action}")
            handled += 1
    except Exception as e:  # noqa: BLE001
        logger.warning("处理权限请求失败：%s", e)

    try:
        for form in oc.forms(session_id):
            meta = form.get("metadata") or {}
            if meta.get("kind") != "question":
                continue                      # 其他表单（外部链接等）不碰
            fields = form.get("fields") or []

            # 优先走 v2 结构化提问卡：手机钉钉上能真正点选。
            # 拿不到答案（超时 / 含自由文本字段 / 卡片不可用）再回退到代答。
            picked = _ask_via_card(writer, fields)
            if picked is not None:
                try:
                    oc.reply_form(session_id, form.get("id"), picked)
                except Exception as e:  # noqa: BLE001
                    logger.warning("提问卡答案提交失败：%s", e)
                else:
                    logger.info("已用提问卡作答 question 表单：%s -> %s",
                                form.get("id"), picked)
                    handled += 1
                    continue

            answer = {}
            for i, f in enumerate(fields):
                key = f.get("key") or f"q{i}"
                answer[key] = FORM_UNSUPPORTED_ANSWER
            try:
                oc.reply_form(session_id, form.get("id"), answer)
            except Exception as e:  # noqa: BLE001
                # 选项校验不通过（非 custom 字段）时退回第一个可选项
                first = {}
                for i, f in enumerate(fields):
                    opts = f.get("options") or []
                    key = f.get("key") or f"q{i}"
                    if opts:
                        first[key] = opts[0].get("value") or opts[0].get("label")
                logger.info("表单直接应答失败（%s），改用首个选项重试", e)
                oc.reply_form(session_id, form.get("id"), first)
            logger.info("自动应答 question 表单：%s（%d 字段）", form.get("id"),
                        len(fields))
            if writer:
                writer.note("💬 已代答交互式提问（请改用文字问）")
            handled += 1
    except Exception as e:  # noqa: BLE001
        logger.warning("处理挂起表单失败：%s", e)

    return handled


# ---------------------------------------------------------------- 并发闸门
class TurnGate:
    """同时处理的消息数上限 + 同一会话串行化。

    为什么不无脑开线程：钉钉连发几条消息就会同时打 OpenCode、抢模型配额，
    而且同一会话并发投递会互相踩上下文。这里：
      * 同一 conversation 已有任务在跑 → 直接拒绝（提示可 /stop）；
      * 全局并发达到上限 → 拒绝（提示稍后重试）。
    """

    def __init__(self, max_concurrent=3):
        self.max_concurrent = max(1, int(max_concurrent))
        self._sem = threading.BoundedSemaphore(self.max_concurrent)
        self._busy: set[str] = set()
        self._lock = threading.Lock()

    def acquire(self, key: str):
        """返回 (ok, 原因文本)。"""
        with self._lock:
            if key in self._busy:
                return False, ("⏳ 上一条还在处理中。\n\n"
                              "等它结束，或发 `/stop` 打断它。")
            if len(self._busy) >= self.max_concurrent:
                return False, (f"⏳ 同时处理的任务已达上限（{self.max_concurrent}）。\n\n"
                               "稍等片刻再发，或发 `/stop` 打断正在跑的任务。")
            self._busy.add(key)
        return True, ""

    def release(self, key: str):
        with self._lock:
            self._busy.discard(key)

    @property
    def busy_count(self) -> int:
        with self._lock:
            return len(self._busy)


# ---------------------------------------------------------------- 文本处理
def local_file_uri(path):
    """OpenCode 只接受 file:/// 形式的附件 uri。"""
    return "file:///" + os.path.abspath(path).replace("\\", "/")


def _parse_table_row(line):
    s = line.strip()
    if not (s.startswith("|") and s.endswith("|")):
        return None
    return [c.strip() for c in s.strip("|").split("|")]


def _is_sep_row(cells):
    return bool(cells) and all(not c or set(c) <= set("-: ") for c in cells)


def _emit_table(block, out):
    """markdown 表格 -> 「• **标签**：说明」列表（丢弃表头）。"""
    rows = [_parse_table_row(l) for l in block]
    rows = [r for r in rows if r is not None]
    if not rows:
        return
    body = [r for r in rows if not _is_sep_row(r)]
    if len(body) >= 2:
        body = body[1:]
    for r in body:
        if len(r) >= 2:
            label, rest = r[0], " · ".join(x for x in r[1:] if x)
            if label:
                out.append(f"• **{label}**：{rest}" if rest else f"• **{label}**")
            elif rest:
                out.append(f"• {rest}")
        elif r and r[0]:
            out.append(f"• {r[0]}")
    out.append("")


def normalize_markdown(text):
    """把模型输出转成钉钉卡片真正能渲染的 markdown 子集。"""
    out, table_buf = [], []

    def flush_table():
        if table_buf:
            _emit_table(list(table_buf), out)
            table_buf.clear()

    for raw in (text or "").split("\n"):
        line = raw.rstrip()
        stripped = line.strip()

        if stripped.startswith("|") and stripped.endswith("|"):
            table_buf.append(line)
            continue
        flush_table()

        if stripped.startswith("```"):
            continue
        m = re.match(r"^\s{0,3}(#{1,6})\s+(.*)$", line)
        if m:
            out.append(f"**{m.group(2).strip()}**")
            continue
        if re.match(r"^\s{0,3}(-{3,}|\*{3,}|_{3,})\s*$", line):
            out.append("")
            continue
        m = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
        if m:
            out.append(f"{m.group(1)}• {m.group(2).strip()}")
            continue
        m = re.match(r"^(\s*)(\d+)[.)]\s+(.*)$", line)
        if m:
            out.append(f"{m.group(1)}{m.group(2)}、{m.group(3).strip()}")
            continue
        line = re.sub(r"`([^`]*)`", r"\1", line)
        line = re.sub(r"~~(.+?)~~", r"\1", line)
        out.append(line)
    flush_table()

    text2 = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text2).strip()


def reply_markdown_chunked(handler, title, text, msg, limit=None):
    """降级路径专用：把超长 markdown **按码点分片**后逐条发出。

    背景：普通 markdown 消息（非卡片）单个正文块也有隐性长度上限
    （实测约 3800 码点，超限时接口成功但客户端渲染成空白）。以前这里直接
    整段发出去，长回答会变成「空白消息」。现在统一走分片，并在标题上标
    page(n/m) 让用户知道还有续片。

    返回实际发出的条数。
    """
    text = text or ""
    lim = int(limit or (getattr(card_v2, "MESSAGE_CHAR_LIMIT", 3800) if card_v2
                        else 3800))
    if card_v2 is not None:
        parts = [c for c, _ in card_v2.split_markdown(text, lim)]
    else:
        parts = [text[i:i + lim] for i in range(0, max(1, len(text)), lim)] or [""]
    parts = parts or [""]
    if len(parts) == 1:
        handler.reply_markdown(title, text, msg)
        return 1
    for i, part in enumerate(parts, 1):
        handler.reply_markdown(f"{title} ({i}/{len(parts)})", part, msg)
    return len(parts)


def render_footer(parts, elapsed=None, model=None, notes=None, status=None,
                  summary=None):
    """紧凑页脚：工具链 + 状态 + 摘要 + 耗时/模型 + 提示。回答区保持干净。"""
    labels, seen, running = [], set(), None
    for p in parts:
        if not (isinstance(p, dict) and p.get("type") == "tool"):
            continue
        name = p.get("name") or "tool"
        label = TOOL_LABELS.get(name, name)
        state = (p.get("state") or {}).get("status", "")
        if state == "running":
            running = label
        if label not in seen:
            seen.add(label)
            labels.append(label)
    labels = labels[-MAX_PROGRESS_LINES:]

    rows = []
    if labels:
        rows.append("🔧 " + " · ".join(labels))
    line = status or (f"⏳ 正在 {running}" if running else None)
    if line:
        rows.append(line)
    if summary:
        rows.append(summary)
    for n in (notes or [])[-3:]:
        rows.append(n)
    if elapsed is not None:
        meta = f"⏱ {elapsed:.1f}s"
        if model:
            meta += f" · {model}"
        rows.append(meta)
    return "\n".join(rows)


def extract_parts(msg):
    parts = msg.get("content") or []
    text = "".join(p.get("text") or "" for p in parts
                   if isinstance(p, dict) and p.get("type") == "text").strip()
    return parts, text


def latest_thought(parts, limit=None):
    """从 parts 里取**最新一句**思考内容（reasoning part 的尾部）。

    为什么需要：卡片上原本只有一句「💭 正在思考」，用户不知道我在看什么、
    在算什么，长任务里等待感特别强。OpenCode 的 assistant 消息里带着
    type=reasoning 的 part（一直跟着 REST 同步回来），只是 extract_parts
    只挑 text，思考被整段丢掉了。

    只取最后一句：整段思考又长又碎，手机上一屏塞不下也没重点。
    """
    limit = CARD_THOUGHT_MAX if limit is None else limit
    if limit <= 0:
        return ""
    for p in reversed(parts or []):
        if not (isinstance(p, dict) and p.get("type") in ("reasoning", "thinking")):
            continue
        t = p.get("text") or p.get("content") or ""
        if isinstance(t, str) and t.strip():
            return t.strip()[-400:]      # 只留尾部：最新的思考在后面
    return ""


def process_summary(parts, elapsed=None, failed=False):
    """终态页脚的一行过程摘要：让卡片收尾后仍看得到「走了几步、哪步崩了」。

    以前 finish() 会把活动面板**整块删掉**，回答一出来过程全没了——事后问
    「你刚才改了哪些文件」只能靠翻日志。摘要只留一行，不占正文版面。
    """
    tools = [p for p in (parts or [])
             if isinstance(p, dict) and p.get("type") == "tool"]
    bits = []
    if failed:
        idx, bad = 0, None
        for i, p in enumerate(tools, 1):
            if card_v2.tool_state(p) in ("error", "failed"):
                idx, bad = i, p
                break
        if bad is not None:
            what = (card_v2.tool_action(bad)
                    or card_v2.tool_label(bad.get("name") or ""))
            bits.append(f"✗ 第 {idx} 步「{what}」失败")
        elif tools:
            bits.append("✗ 执行失败")
    if tools:
        bits.append(f"{len(tools)} 个工具调用")
    if elapsed is not None:
        el = max(0.0, float(elapsed))
        bits.append(f"{int(round(el))} 秒" if el < 60
                    else f"{int(el // 60)} 分 {int(el % 60)} 秒")
    return " · ".join(bits)


def is_quota_error(msg) -> bool:
    """判断一条 assistant 消息是否是"主模型额度/限流耗尽"这类不可用错误。

    两条硬性前提，缺一不可：
    1. 这条消息**本身就是错误**（finish == "error" 或带 error 字段）；
       正常回答（finish == "stop"）无论提到什么词都**不算**——否则一段
       聊到"额度"的正常回复会把模型误切走。
    2. 错误内容里命中额度/限流/欠费类关键词。
    """
    if not msg:
        return False

    has_error = (msg.get("finish") == "error") or bool(msg.get("error"))
    blob = []
    err = msg.get("error")
    if isinstance(err, dict):
        blob.append(json.dumps(err, ensure_ascii=False))
    elif err:
        blob.append(str(err))
    for p in msg.get("content") or []:
        if isinstance(p, dict):
            if p.get("type") == "text":
                blob.append(p.get("text") or "")
            elif p.get("type") in ("error", "reasoning-error"):
                has_error = True
                blob.append(json.dumps(p, ensure_ascii=False))
            if p.get("error"):
                has_error = True
                blob.append(str(p["error"]))
    if not has_error:
        return False
    s = " ".join(blob).lower()
    return any(k in s for k in _QUOTA_ERROR_PAT)


# ---------------------------------------------------------------- 会话映射
class SessionMap:
    """钉钉会话 -> OpenCode session 的持久映射，带轮换计数。

    为什么要轮换：session 是**持久化**的，重启机器人不会新建，历史会一直累积。
    消息条数撞到服务端 200 条硬上限后，上下文就废了。所以自己记次数，
    超过 ROTATE_TURNS 就换一个全新 session（旧的保留，仍可在 OpenCode 里查看）。

    存 {sid, turns} 结构；同时兼容早期只存字符串的格式。
    """

    def __init__(self, path):
        self.path = path
        self.data = {}
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            for k, v in (raw or {}).items():
                if isinstance(v, str):
                    self.data[k] = {"sid": v, "turns": 0}
                elif isinstance(v, dict) and v.get("sid"):
                    self.data[k] = {"sid": v["sid"], "turns": int(v.get("turns") or 0)}
        except (OSError, json.JSONDecodeError):
            self.data = {}

    def get(self, conv_id):
        rec = self.data.get(conv_id)
        return rec["sid"] if rec else None

    def turns(self, conv_id):
        rec = self.data.get(conv_id)
        return rec["turns"] if rec else 0

    def set(self, conv_id, sid, turns=0):
        self.data[conv_id] = {"sid": sid, "turns": int(turns)}
        self.save()

    def bump(self, conv_id):
        """记一次对话，返回新的次数。"""
        rec = self.data.get(conv_id)
        if rec:
            rec["turns"] = int(rec.get("turns") or 0) + 1
            self.save()
            return rec["turns"]
        return 0

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def items(self):
        return [(k, v["sid"]) for k, v in self.data.items()]

    def rotate(self, conv_id, new_sid):
        """换新会话；把旧 session id 记进 history 方便追溯。"""
        rec = self.data.get(conv_id) or {}
        old = rec.get("sid")
        hist = list(rec.get("history") or [])
        if old:
            hist.append({"sid": old, "turns": int(rec.get("turns") or 0)})
        self.data[conv_id] = {"sid": new_sid, "turns": 0,
                              "history": hist[-10:]}
        self.save()

    def reset(self):
        """清空全部映射——下次对话会开一个全新会话。

        用于「机器人重启后上下文清零」。旧 session 不会被删除，
        仍可在 OpenCode 里查看历史。
        """
        n = len(self.data)
        self.data = {}
        self.save()
        return n


# ---------------------------------------------------------------- 一轮完整交互
class AgentTurn:
    """执行一次「钉钉消息 -> OpenCode agent -> AI 卡片」的完整流程。

    handler 需提供：.dingtalk_client、.get_image_download_url(code)、
    .reply_markdown(title, text, msg)
    """

    def __init__(self, handler, incoming_message, *, work_dir, data_dir,
                 model=None, title="OpenCode", poll_interval=POLL_INTERVAL):
        self.handler = handler
        self.msg = incoming_message
        self.work_dir = work_dir
        self.data_dir = data_dir
        self.model = model or DEFAULT_MODEL
        self.title = title
        self.poll_interval = poll_interval
        self.sessions = SessionMap(os.path.join(data_dir, "sessions.json"))
        self.image_dir = os.path.join(data_dir, "images")
        self._carry = ""          # 轮换时上一段对话的交接摘要
        os.makedirs(self.image_dir, exist_ok=True)

    # ---- 会话 ----
    def _new_session(self, oc, conv, title):
        sess = oc.create_session(self.work_dir, title=title, model=self.model)
        sid = sess["id"]
        apply_instructions(oc, sid)
        return sid

    def _handover(self, oc, old_sid):
        """轮换前让旧会话自己总结一段交接说明，好让新会话接得上。

        失败/超时都返回空串——拿不到摘要也只是丢一点上下文，
        绝不能因此卡住用户这一轮。
        """
        ask = ("请用不超过 150 字总结我们刚才这段对话，供下一个会话接手。只写这四项："
               "1) 用户的持仓/在做的操作（点位、方向、成本、止损）"
               "2) 已确认的关键结论和关键价位 3) 用户的偏好与要求 "
               "4) 尚未解决的问题。不要调用任何工具，直接输出这段文字。")
        try:
            oc.prompt(old_sid, ask)
        except Exception as e:  # noqa: BLE001
            logger.warning("交接摘要 prompt 失败: %s", e)
            return ""
        done = threading.Event()

        def _w():
            try:
                oc.wait_idle(old_sid, timeout=HANDOVER_TIMEOUT)
            finally:
                done.set()

        threading.Thread(target=_w, daemon=True).start()
        if not done.wait(HANDOVER_TIMEOUT + 5):
            logger.warning("交接摘要超时，放弃（直接开新会话）")
            return ""
        try:
            msg = oc.last_assistant(old_sid) or {}
            parts, text = extract_parts(msg)
        except Exception as e:  # noqa: BLE001
            logger.warning("取交接摘要失败: %s", e)
            return ""
        text = (text or "").strip()
        # 模型可能不听话把它写成一大段，截断一下
        return text[:600] if text else ""

    def session_id(self, force_new=False):
        conv = self.msg.conversation_id
        oc = OpenCodeService()
        title = f"dingtalk:{str(conv)[:16]}"

        if not force_new:
            sid = self.sessions.get(conv)
            turns = self.sessions.turns(conv)
            if sid and (not ROTATE_TURNS or turns < ROTATE_TURNS):
                return sid
            if sid:
                # 到阈值了：先摘要再换新会话
                logger.info("会话 %s 已 %d 轮（阈值 %d），准备轮换", sid, turns,
                            ROTATE_TURNS)
                carry = self._handover(oc, sid)
                new_sid = self._new_session(oc, conv, title)
                self.sessions.rotate(conv, new_sid)
                if carry:
                    self._carry = carry
                    logger.info("已轮换到 %s，带交接摘要 %d 字", new_sid, len(carry))
                else:
                    self._carry = ""
                    logger.info("已轮换到 %s（无交接摘要）", new_sid)
                return new_sid

        sid = self._new_session(oc, conv, title)
        self.sessions.set(conv, sid)
        self._carry = ""
        logger.info("new OpenCode session %s for conv %s (model=%s)",
                    sid, conv, self.model["id"])
        return sid

    def ensure_style(self, sid):
        return apply_instructions(OpenCodeService(), sid)

    # ---- 图片 ----
    def _download_images(self, codes):
        files = []
        for i, code in enumerate(codes or [], 1):
            try:
                url = self.handler.get_image_download_url(code)
                if not url:
                    logger.warning("image#%s no downloadUrl", i)
                    continue
                r = requests.get(url, timeout=30)
                r.raise_for_status()
                ctype = r.headers.get("content-type", "image/png").split(";")[0]
                ext = {"image/jpeg": ".jpg", "image/gif": ".gif",
                       "image/webp": ".webp"}.get(ctype, ".png")
                path = os.path.join(self.image_dir,
                                    f"img_{int(time.time())}_{i}{ext}")
                with open(path, "wb") as f:
                    f.write(r.content)
                files.append({"uri": local_file_uri(path),
                              "name": os.path.basename(path)})
                logger.info("image#%s saved %s (%s bytes)", i, path, len(r.content))
            except Exception as e:  # noqa: BLE001
                logger.warning("image#%s download failed: %s", i, e)
        return files

    # ---- 文件 / 音频 / 视频附件 ----
    def _download_files(self, specs):
        """按 downloadCode 下载附件，返回 OpenCode 附件列表。

        钉钉的图片和文件走**同一个**下载接口
        （/v1.0/robot/messageFiles/download），所以这里复用同一个取址方法。

        注意：downloadCode 是一段很长的 base64url，**可能含 `/` 和 `+`**，
        绝不能直接当文件名（会拼出非法路径）。这里只拿它前 16 位做可读前缀，
        文件名用「原始名 + 短哈希」保证唯一且合法。
        """
        import hashlib

        import requests as _rq
        out = []
        for sp in specs or []:
            code = sp["download_code"]
            try:
                url = self.handler.get_image_download_url(code)
                if not url:
                    logger.warning("取下载地址失败: %s", sp.get("fileName"))
                    continue
                r = _rq.get(url, timeout=90)
                r.raise_for_status()

                name = os.path.basename(sp.get("fileName") or "").strip() or "附件"
                name = re.sub(r'[\\/:*?"<>|]', "_", name)[:80]      # 去掉路径分隔等
                stem, ext = os.path.splitext(name)
                if not ext and sp.get("fileType"):
                    ext = "." + str(sp["fileType"]).split("/")[-1]
                digest = hashlib.sha1(code.encode()).hexdigest()[:10]
                path = os.path.join(self.image_dir, f"{stem}_{digest}{ext}")

                with open(path, "wb") as fh:
                    fh.write(r.content)
                out.append({"uri": local_file_uri(path), "name": name})
                logger.info("附件已下载 %s -> %s (%d bytes)", name, path,
                            len(r.content))
            except Exception as e:  # noqa: BLE001
                logger.warning("附件下载失败 %s: %s", sp.get("fileName"), e)
        return out

    # ---- 一轮驱动（事件流 + 兜底轮询）----
    def _run_turn(self, oc, sid, writer, q, prompt_text, files, t0):
        """投递消息 → 边跑边刷卡片 → 等空闲 → 取最终回答。

        实时性来自事件流（session.text.delta / session.tool.*）；
        正确性来自 wait_idle（完成信号）+ 低频兜底轮询（事件流易失会丢）。
        返回 dict：text / finish / model / provider / parts / status / message
        """
        # 清掉上一轮可能残留的事件，避免串味
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                break

        st = {
            "text": "",          # 已结束的文本块拼接
            "cur": "",           # 当前文本块累积的 delta
            "rest_text": "",     # 兜底轮询拿到的全量文本（优先显示）
            "parts": [],         # 工具状态（页脚用，格式同 /message 的 content）
            "tools": {},         # call id -> {"name", "status"}
            "finish": "",
            "model": "",
            "provider": "",
            "status": None,
            "last_event": time.time(),
            "message": None,
        }

        prev_id = None
        try:
            m = oc.last_assistant(sid)
            prev_id = m.get("id") if m else None
        except Exception:  # noqa: BLE001
            pass

        oc.prompt(sid, prompt_text, files=files or None)

        done = threading.Event()

        def _waiter():
            try:
                time.sleep(0.3)
                oc.wait_idle(sid, timeout=WAIT_TIMEOUT)
            except Exception as e:  # noqa: BLE001
                logger.warning("wait_idle error: %s", e)
            finally:
                done.set()

        threading.Thread(target=_waiter, daemon=True).start()

        bus = EventBus.instance()
        last_fallback = last_gate = last_footer = 0.0

        while not done.is_set():
            try:
                ev = q.get(timeout=0.2)
            except queue.Empty:
                ev = None
            if ev is not None:
                self._apply_event(ev, st, writer)

            now = time.time()

            # 明细同步：工具名 / 参数 / 起止时间**只有 REST 的 part 才带全**，
            # 事件流（session.tool.*）只给 id + name。而活动面板要显示
            # 「读取 bridge.py · 1.2s」，就必须每拍拿一次 REST 的 parts。
            # 这是本机调用，几毫秒，比让卡片冻着划算。
            if now - last_fallback >= POLL_FALLBACK_INTERVAL:
                last_fallback = now
                self._sync_from_rest(oc, sid, prev_id, st)

            # 挂起项兜底：权限请求 / question 表单（手机端无法应答，必须代答）
            if now - last_gate >= GATE_CHECK_INTERVAL:
                last_gate = now
                handle_pending_gates(oc, sid, writer)

            # 卡片刷新（防抖 / 限流都在 CardWriter 内部）
            if writer:
                body = self._current_text(st)
                if body:
                    # v2 卡片支持完整 markdown，不做 v1 那套降级转换
                    writer.stream(body if getattr(writer, "passthrough_markdown", False)
                                  else normalize_markdown(body))
                if now - last_footer >= writer.min_interval:
                    last_footer = now
                    # 事件流长时间没动静时，别继续挂着上一条状态（「正在执行」
                    # 挂 7 分钟就是这个效果）。如实显示静默时长——用户至少
                    # 知道是模型还没回话，而不是卡片冻住了。
                    status = st["status"]
                    silent = now - st["last_event"]
                    # 工具在跑 → 30 秒才算沉默（跑命令本来就慢）；纯思考 → 12 秒
                    # 就该如实说「已静默」，否则用户只会觉得卡片卡死了。
                    busy = any((t or {}).get("status") == "running"
                               for t in (st.get("tools") or {}).values())
                    idle_after = CARD_IDLE_AFTER if busy else CARD_IDLE_THINKING
                    if silent >= idle_after:
                        status = f"⏳ 已静默 {int(silent)}s"
                    writer.footer(writer.footer_text(
                        st["parts"], elapsed=now - t0, model=st["model"],
                        status=status))

        # 收尾：把队列里剩下的也吃掉（可能含最后一段文本）
        for _ in range(200):
            try:
                ev = q.get_nowait()
            except queue.Empty:
                break
            self._apply_event(ev, st, writer)

        # 以服务端最终消息为准（事件流可能丢帧）
        self._sync_from_rest(oc, sid, prev_id, st, force=True)

        return {
            "text": self._current_text(st),
            "finish": st["finish"],
            "model": st["model"],
            "provider": st["provider"],
            "parts": st["parts"],
            "status": st["status"],
            "message": st["message"],
        }

    @staticmethod
    def _apply_event(ev, st, writer=None):
        """把一条事件折算成「卡片要显示的内容」。"""
        etype = ev.get("type") or ""
        data = ev.get("data") or {}
        st["last_event"] = time.time()

        if etype == "session.text.started":
            st["cur"] = ""
            st["rest_text"] = ""
            st["status"] = "✍️ 正在写回答"
        elif etype == "session.text.delta":
            st["cur"] += data.get("delta") or ""
        elif etype == "session.text.ended":
            st["text"] += data.get("text") or st["cur"]
            st["cur"] = ""
            st["status"] = "🤔 正在思考"
        elif etype == "session.tool.input.started":
            st["tools"][data.get("id")] = {"name": data.get("name") or "tool",
                                           "status": "running"}
            # 注意：**不要**在这里用 name-only 的 parts 覆盖 st["parts"]。
            # 事件只带 id+name，没有 input/time，覆盖一次就等于把活动面板里
            # 「读取 bridge.py · 1.2s」降级成「读取文件」。parts 统一由
            # _sync_from_rest 每 3 秒取一次（带 state.input / time）。
            st["status"] = "🔧 正在执行"
            st["rest_text"] = ""
            # 面板要**立刻**出现：事件只带工具名，先用它占一行，明细随后覆盖。
            # 用 tool_action（空 input）拿「执行命令 / 读取文件」这类泛化说法，
            # 不用 tool_label —— 后者是给页脚做种类统计的，"bash → 执行检查"
            # 挂在「正在干什么」上会误导。
            if card_v2 and writer is not None and hasattr(writer, "hint_action"):
                try:
                    name = data.get("name") or ""
                    hint = (card_v2.tool_action(
                        {"name": name, "state": {"input": {}}})
                        or card_v2.tool_label(name))
                    writer.hint_action(hint)
                except Exception:  # noqa: BLE001
                    pass
        elif etype in ("session.tool.success", "session.tool.failed"):
            t = st["tools"].get(data.get("id"))
            if t:
                t["status"] = "completed"
            if not any(x["status"] == "running" for x in st["tools"].values()):
                # 工具都跑完了 → 模型回到思考。状态行不回到「思考」，就会
                # 一直挂着「正在执行」，几十分钟不动，看着就是卡死。
                st["status"] = "🤔 正在思考"
        elif etype == "session.step.started":
            model = data.get("model") or {}
            st["model"] = model.get("id") or st["model"]
            st["provider"] = model.get("providerID") or st["provider"]
        elif etype == "session.step.ended":
            st["finish"] = data.get("finish") or st["finish"]
        elif etype == "session.execution.interrupted":
            st["status"] = "🛑 已中断"
            if writer:
                writer.note("🛑 已中断")
        elif etype in ("session.execution.failed", "session.error"):
            st["status"] = "❌ 执行失败"

    @staticmethod
    def _sync_from_rest(oc, sid, prev_id, st, force=False):
        """兜底：用 REST 拿最新 assistant 消息，补齐事件流可能丢的内容。

        REST 返回的是**全量**文本，单独存 rest_text，不覆盖事件累积的文本，
        避免「REST 全量 + 事件增量」拼成重复内容。
        """
        try:
            m = oc.last_assistant(sid)
        except Exception as e:  # noqa: BLE001
            if force:
                logger.warning("取最终回答失败: %s", e)
            return
        if not m or (not force and m.get("id") == prev_id):
            return
        parts, reply = extract_parts(m)
        if reply:
            st["rest_text"] = reply
        if parts:
            st["parts"] = parts
        st["finish"] = m.get("finish") or st["finish"]
        model = m.get("model") or {}
        st["model"] = model.get("id") or st["model"]
        st["provider"] = model.get("providerID") or st["provider"]
        st["message"] = m

    @staticmethod
    def _current_text(st):
        """当前该显示的正文：REST 全量优先（权威），否则用事件累积。"""
        from_events = (st["text"] + st["cur"]).strip()
        return st.get("rest_text") or from_events

    # ---- 主流程 ----
    def execute(self, text, image_codes=None, file_specs=None, quote=None):
        t0 = time.time()
        writer = None
        msg = self.msg
        try:
            sid = self.session_id()
            oc = OpenCodeService()

            files = self._download_images(image_codes)
            want = len(image_codes or []) + len(file_specs or [])
            files += (self._download_files(file_specs) if file_specs else [])
            if want and not files:
                # 附件一个都没落地：如实告诉 agent，让它转述失败原因，
                # 而不是回答"没收到内容"这种误导性的话
                logger.error("附件全部下载失败（共 %d 个）", want)
                failed_names = "、".join(
                    (sp.get("fileName") or "附件") for sp in (file_specs or []))
                files = []
                prompt_text = (text or "").strip()
                note = (f"（用户发送了 {want} 个附件{failed_names}，"
                        f"但**下载失败**，无法读取内容）")
                prompt_text = f"{prompt_text}\n\n{note}" if prompt_text else note
            else:
                prompt_text = (text or "").strip()
                if files and not prompt_text:
                    names = "、".join(f["name"] for f in files)
                    prompt_text = f"请看这份附件：{names}。"
                elif files and prompt_text:
                    names = "、".join(f["name"] for f in files)
                    prompt_text = f"{prompt_text}\n\n（随附附件：{names}）"
            if not prompt_text:
                prompt_text = "（空消息）"

            # 引用回复（钉钉回传 text.isReplyMsg / text.repliedMsg）：把被引用
            # 卡片的完整问答拼进上下文——钉钉只给一段摘要，真正"回到那张卡的
            # 上下文"得靠本地档案，见 build_quote_context。
            quote_ctx = build_quote_context(quote, msg.conversation_id)
            if quote_ctx:
                prompt_text = f"{quote_ctx}\n————\n用户现在说：{prompt_text}"

            # 记住用户原话：卡片上的「重试」按钮要靠它重放。
            # 存拼好的 prompt（含引用上下文），重试才是原样重放，不会丢引用。
            LAST_PROMPT.remember(msg.conversation_id, prompt_text)

            # 轮换后的新会话：把上一段对话的交接摘要带过来，保证连续性
            if self._carry:
                prompt_text = (f"【上一段对话的交接摘要】\n{self._carry}\n\n"
                               f"———\n用户现在说：{prompt_text}")
                self._carry = ""
            self.sessions.bump(self.msg.conversation_id)

            writer = make_writer(self.handler, msg, self.title)
            if writer is None:
                logger.warning("AI card unavailable -> fallback to plain markdown")
                self.handler.reply_markdown("⏳ 处理中", "已转交 OpenCode，请稍候…", msg)

            # 登记「在飞回合」：带上卡片 id。进程若被重启/强杀，新进程据此
            # 把这张卡片收尾（补上回答或标注「重启前未完成」），
            # 用户就不会看到一张永远停在「正在思考…」的僵尸卡。
            # 注意记下卡片类型：v2 卡片必须用 v2 接口收尾（见 recover_inflight）。
            INFLIGHT.begin(msg.conversation_id, sid,
                           writer.card.card_instance_id if writer else None,
                           self.title, text,
                           card_v2=isinstance(writer, CardWriterV2))

            # 事件流：负责「正在想什么 / 正在写什么」的实时呈现；
            # wait_idle + 低频兜底轮询负责正确性（事件流是易失的，会丢事件）。
            bus = EventBus.instance()
            q = bus.subscribe(sid)
            try:
                run = self._run_turn(oc, sid, writer, q, prompt_text, files, t0)

                # ---- 主模型额度/限流耗尽 → 自动切到兜底模型重来一次 ----
                cur_msg = run.get("message")
                cur_pid = run.get("provider") or ""
                if (cur_msg and is_quota_error(cur_msg)
                        and cur_pid != FALLBACK_MODEL["providerID"]):
                    logger.warning("主模型不可用（%s/%s），切换兜底模型 %s/%s",
                                   cur_pid, run.get("model"),
                                   FALLBACK_MODEL["providerID"],
                                   FALLBACK_MODEL["id"])
                    try:
                        oc.switch_model(sid, FALLBACK_MODEL["providerID"],
                                        FALLBACK_MODEL["id"])
                    except Exception as e:  # noqa: BLE001
                        logger.error("切换兜底模型失败: %s", e)
                    else:
                        if writer:
                            writer.note(
                                f"⚠️ 主模型额度不足，已切到 {FALLBACK_MODEL['id']}")
                        run = self._run_turn(oc, sid, writer, q, prompt_text,
                                             files, t0)
            finally:
                bus.unsubscribe(sid, q)

            elapsed = time.time() - t0
            interrupted = bool(run.get("status") and "中断" in run["status"])
            final_text = (run.get("text") or "").strip()
            if not final_text:
                final_text = "🛑 已按你的要求中断。" if interrupted else "（空回复）"
            # v2 卡片会把超长回答分片成多个 block，不再粗暴截断丢内容；
            # v1（SDK 卡片，总量硬上限 10KB）仍然要截断。
            v2_mode = bool(writer and getattr(writer, "passthrough_markdown", False))
            if not v2_mode and len(final_text) > MAX_ANSWER_CHARS:
                final_text = final_text[:MAX_ANSWER_CHARS] + "\n\n…（内容过长已截断）"

            # 归档这一轮的「卡片 id → 提问 + 回答」：用户以后引用这张卡回复时，
            # build_quote_context 靠它把完整历史上下文捞回来（跨重启有效）。
            if writer is not None:
                try:
                    CARD_ARCHIVE.remember(
                        conv=msg.conversation_id, question=text, answer=final_text,
                        card_id=getattr(writer.card, "card_instance_id", "") or "",
                        out_track_id=getattr(writer.card, "out_track_id", "") or "",
                        session_id=sid)
                except Exception as e:  # noqa: BLE001
                    logger.warning("卡片问答归档失败：%s", e)

            finish = run.get("finish") or ""
            model_id = run.get("model") or ""
            final_parts = run.get("parts") or []
            # 用户主动打断不是「出错」，别把卡片标成失败
            failed = (finish == "error") and not interrupted

            logger.info("agent done conv=%s finish=%s chars=%s elapsed=%.1fs "
                        "stream=%s interrupted=%s", msg.conversation_id, finish,
                        len(final_text), elapsed,
                        "事件流" if bus.connected else "轮询兜底", interrupted)

            # 收尾状态：事件流的 status 是「只写不擦」的（text.started 设成
            # 「正在写回答」后 text.ended/step.ended 都不重置），会残留到页脚，
            # 让已完成的卡片还显示「✍️ 正在写回答」。这里强制换成终态。
            final_status = "✅ 完成"
            if interrupted:
                final_status = "🛑 已中断"
            elif finish == "error":
                final_status = "❌ 执行失败"
            footer = render_footer(final_parts, elapsed=elapsed, model=model_id,
                                   notes=(writer.notes if writer else None),
                                   status=final_status,
                                   summary=process_summary(final_parts,
                                                           failed=failed))
            if writer and writer.finish(
                    final_text if v2_mode else normalize_markdown(final_text),
                    footer, failed=failed, parts=final_parts):
                pass
            else:
                reply_markdown_chunked(self.handler, "✅ 完成", final_text, msg)
                self.handler.reply_markdown(
                    "Info", f"耗时 {elapsed:.1f}s · {model_id}", msg)

            return {"text": final_text, "finish": finish, "elapsed": elapsed,
                    "model": model_id, "card": writer is not None}
        except Exception as e:  # noqa: BLE001
            logger.exception("agent turn failed")
            try:
                if writer:
                    writer.finish("（出错了，没有拿到回答）", failed=True)
                self.handler.reply_markdown("❌ 出错",
                                            f"`{type(e).__name__}: {e}`", msg)
            except Exception:  # noqa: BLE001
                pass
            return {"text": "", "finish": "error", "elapsed": time.time() - t0,
                    "model": "", "card": writer is not None}
        finally:
            # 回答已投递（或出错已兜底），撤掉在飞登记。
            # 漏撤也无害：下次启动只会多做一次收尾。
            INFLIGHT.end(msg.conversation_id)
