"""中证1000 交易助手机器人 v3 —— **纯 OpenCode 桥接**。

设计（大幅整改后）：
    钉钉消息 ──► OpenCode agent（唯一大脑）──► 钉钉 AI 卡片

- **没有任何意图正则路由**：一切都交给 agent，它自己决定要不要调数据工具。
- 行情/点位/快讯的数据由 MCP 工具提供（tools/market_mcp.py）：
    market_quote（实时行情）/ market_overseas（隔夜外围）/ market_wave（道氏+波浪，确定性计算）
    / market_tech（多周期技术面）/ market_news（消息面）
  **分析由 agent 自己完成**（market_wave 是确定性计算的例外，不是黑盒 LLM）。
- 写作规范 / 交易框架统一放在 Location 的 `.opencode-bot/AGENTS.md`，
  会话里只留一条精简冗余提示（见 tools/opencode_bridge.py 的 BRIEF_TEXT）。
- **实时性**：桥接层订阅 OpenCode 事件流（SSE /api/event）拿 text.delta / tool.*
  逐字刷卡片，wait_idle + 低频兜底轮询保证正确性（事件流是易失的）。
- **卡片写入**：800ms 防抖 + 全局 20 QPS 令牌桶 + 撞 403/QpsLimit 退避重试
  + 10KB 截断（钉钉 AI 卡片流式更新的硬限制）。
- **手机端无法应答交互项**：question 工具在配置里 deny；权限请求 /
  question 表单由桥接层自动代答（低风险放行、其余拒绝并提示用户）。
- **并发保护**：同一会话串行化，同时最多 BOT_MAX_CONCURRENT 个任务。
- **命令**：/new /clear /sid /model /stop /compact /fork /restart /help。
- **重启方式**：协同重启——/restart 或 tools/bot_restart.py 只写「重启标记」，
  监听进程在**当前这一轮跑完、卡片收尾之后**才退出，由看门狗拉起。
  **绝不在自己这一轮里 kill 自己**（那会把正在渲染的卡片变成僵尸卡）。
- **重启找回**：在飞回合（含卡片 id）登记在 bridge_data/inflight.json；
  新进程启动时把遗留卡片收尾（补上真实回答或标注「重启前未完成」）。
- **状态告知**：停机期间钉钉不会补发消息，所以三个时间点主动推通知
  （tools/bot_presence.py）——重启前「正在重启」、意外掉线由看门狗推「掉线了」、
  新进程起来推「已恢复（中断 N 秒，没回音的重发）」；`/status` 可随时查状态。
- **单实例保护**：钉钉 Stream 同一 APP_KEY 只能有一个连接，故用命名互斥体防重复启动。
- 已移除：意图识别、行情专用快路径、微信授权巡检 / 二维码推送 / 每日 14:50 定时任务。

出问题可回退：旧项目 D:\PythonProject\MainToy\finanCalc 里的同名前身
（listen_up_dingtalk_v3.py，仍在原仓库里未删）。
"""
import atexit
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

from dotenv import load_dotenv

# 日志中文编码：必须在任何 logging handler 建立之前
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

HERE = os.path.dirname(os.path.abspath(__file__))          # .../dingtalk-agent/bot
MAINTOY = os.path.dirname(HERE)                            # .../dingtalk-agent
load_dotenv(os.path.join(MAINTOY, ".env"))
sys.path.insert(0, MAINTOY)

from dingtalk_stream import AckMessage, Credential, DingTalkStreamClient  # noqa: E402
from dingtalk_stream.chatbot import ChatbotHandler, ChatbotMessage  # noqa: E402
from dingtalk_stream.card_callback import (  # noqa: E402
    Card_Callback_Router_Topic, CardCallbackMessage,
)
from dingtalk_stream.handlers import CallbackHandler  # noqa: E402

from tools import bot_presence  # noqa: E402
from tools.opencode_bridge import (  # noqa: E402
    CARD_V2_OK, MODEL_CHOICES, AgentTurn, OpenCodeService,
    SessionMap, TurnGate, apply_instructions, card_v2, clear_restart,
    dispatch_card_callback, model_name, recover_inflight, request_restart,
    restart_pending, restart_request, set_retry_hook, start_model_choice,
    switch_model_checked,
)

APP_KEY = os.getenv("APP_KEY")
APP_SECRET = os.getenv("APP_SECRET")

# ========== 只允许这些钉钉用户驱动 agent（防止本机被未授权访问） ==========
# 默认值保持原样；需要多人/换人时用环境变量覆盖：
#   DINGTALK_ALLOWED_STAFF_IDS="id1,id2"
ALLOWED_STAFF_IDS = {
    s.strip() for s in (
        os.getenv("DINGTALK_ALLOWED_STAFF_IDS") or "01236018344132183746"
    ).split(",") if s.strip()
}


def _env_int(name, default):
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


# 单人限速：RATE_LIMIT_WINDOW 秒内最多 RATE_LIMIT_MAX 条（防连点、防炸模型配额）
RATE_LIMIT_MAX = _env_int("DINGTALK_RATE_LIMIT", 20)
RATE_LIMIT_WINDOW = 60.0
# 同时处理的消息数上限（超出直接拒绝，提示可 /stop）
MAX_CONCURRENT_TURNS = _env_int("DINGTALK_MAX_CONCURRENT", 3)
TURN_GATE = TurnGate(MAX_CONCURRENT_TURNS)

# ========== OpenCode 桥接配置 ==========
# 专属 Location：只有这个目录的配置里注册了 market MCP，
# 所以行情工具只对机器人可见，用户平时的 TUI 看不到。
_DEFAULT_BOT_DIR = os.path.join(MAINTOY, ".opencode-bot")
OPENCODE_WORK_DIR = os.getenv("OPENCODE_WORK_DIR") or _DEFAULT_BOT_DIR
BRIDGE_DATA_DIR = os.getenv("BRIDGE_DATA_DIR") or os.path.join(HERE, "bridge_data")
LOG_FILE = os.path.join(HERE, "logs", "dingtalk_v3.log")

# ========== 接收方会话持久化 ==========
# 只写一份「最近活跃用户」缓存，供 tools/notifycation.send_dingtalk_image 使用
# （那是 fetch_articles.py 自己的二维码推送流程在用，跟本机器人无关）。
RECIPIENT_FILE = os.path.join(MAINTOY, "tools", ".dingtalk_recipient.json")

logger = logging.getLogger("dingtalk_v3")


# 卡片类被引用消息（msgType=interactiveCard）的正文是一棵**多层容器树**：
#     content → cardContent[] → {children: [{children: [{elementType, value}, ...]}]}
# 真正要取的 TEXT 全在 `value` 里，但外面套了好几层 children，所以容器键必须
# 显式下钻（见 _flatten_quoted）。深度上限也要给够——之前是 4，正文刚好卡在
# 第 5 层被整段丢掉，用户引用卡片时只剩标题行「✅ 完成 / ⏱ …s」，正文全没了。
_QUOTE_CONTAINER_KEYS = ("cardContent", "children", "elements", "body",
                         "rows", "cols", "items")
# 结构噪声：这些键的值是样式/类型标记，不是正文，别混进引用文本里
_QUOTE_NOISE_KEYS = ("elementType", "elementId", "type", "id", "style",
                     "align", "flexMode", "cardInstanceId", "outTrackId",
                     "cardId", "msgId", "createdAt", "senderId", "msgType")


def _flatten_quoted(node, depth=0) -> str:
    """把 repliedMsg.content 里各式各样的结构压成一段纯文本。

    钉钉对不同消息类型给不同形状：text 给 {"text": ...}，markdown/富文本给
    {"text": ...}/{"content": [...]}，卡片类给 cardContent 容器树（见上面注释）。
    这里只求"能取多少取多少"，取不到就返回空（上游会如实告诉用户）。
    """
    if node is None or depth > 20:          # 卡片正文能套到 5~6 层，给足余量
        return ""
    if isinstance(node, str):
        return node.strip()
    if isinstance(node, (int, float)):
        return str(node)
    if isinstance(node, list):
        return " ".join(x for x in (_flatten_quoted(i, depth + 1) for i in node) if x)
    if isinstance(node, dict):
        parts = []
        for key in ("text", "content", "value", "title", "summary",
                    "richText", "markdown", "plainText"):
            if key in node:
                parts.append(_flatten_quoted(node[key], depth + 1))
        if not parts:                       # 正文键都没有 → 先下钻容器键
            for key in _QUOTE_CONTAINER_KEYS:
                if key in node:
                    parts.append(_flatten_quoted(node[key], depth + 1))
        if not parts:                       # 兜底：字典里其余值都试一遍
            parts = [_flatten_quoted(v, depth + 1) for k, v in node.items()
                     if k not in _QUOTE_NOISE_KEYS]
        return " ".join(p for p in parts if p)
    return ""


def _extract_quote(data: dict) -> dict:
    """取出「引用回复」信息。没引用返回 {}。

    钉钉在用户引用某条消息回复时，把被引用内容塞进 **text 对象**（不是顶层）：
        "text": {"content": "用户打的话", "isReplyMsg": true,
                 "repliedMsg": {"msgId": "...", "msgType": "text",
                                "content": {"text": "被引用原文"} | "原文"}}
    SDK 的 TextContent 只认 `content`，其余字段落进 extensions，所以这里和
    _extract_file_specs 一样直接看**原始 dict**（顺带拿到 cardContent 之类的
    额外字段，交给桥接层做卡片 id 精确匹配）。
    """
    text_obj = (data or {}).get("text")
    if not isinstance(text_obj, dict):
        return {}
    replied = text_obj.get("repliedMsg")
    if not text_obj.get("isReplyMsg") and not replied:
        return {}
    if not isinstance(replied, dict):
        replied = {}
    content = replied.get("content")
    card_id = ""
    if isinstance(content, dict):
        card_id = (content.get("outTrackId") or content.get("cardInstanceId")
                   or content.get("cardId") or "")
    return {
        "is_reply": True,
        "msg_type": replied.get("msgType") or replied.get("messageType") or "",
        "msg_id": replied.get("msgId") or replied.get("messageId") or "",
        "card_id": card_id,
        # 卡片被引用时正文就靠这一份（拿不到时上游再退回本机卡片档案），
        # 上限与 build_quote_context 的回答上限（6000）对齐，别把长回答腰斩。
        "text": _flatten_quoted(content)[:6000],
        "raw": replied,
    }


def _persist_recipient(msg: ChatbotMessage) -> None:
    import json
    try:
        payload = {
            "sender_staff_id": msg.sender_staff_id,
            "sender_nick": msg.sender_nick,
            "robot_code": msg.robot_code,
            "session_webhook": msg.session_webhook,
            "session_webhook_expired_time": msg.session_webhook_expired_time,
            "conversation_id": msg.conversation_id,
            "conversation_type": msg.conversation_type,
            "conversation_title": msg.conversation_title,
            "updated_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(RECIPIENT_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        logging.warning("持久化接收方会话信息失败: %s", e)


def setup_logger():
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(name)-8s %(levelname)-8s %(message)s [%(filename)s:%(lineno)d]")
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    root = logging.getLogger()
    for h in list(root.handlers):      # 清掉第三方库导入时留下的 handler
        root.removeHandler(h)
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)
    root.setLevel(logging.INFO)
    return logger


def _disable_dead_proxy():
    """系统 HTTP_PROXY 常指向未启动的代理，会让钉钉连接和行情请求全部超时。
    启动时探测一次，连不上就清掉。"""
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        val = os.environ.get(var)
        if not val:
            continue
        try:
            u = urlparse(val)
            with socket.create_connection((u.hostname, u.port or 80), timeout=0.5):
                pass
        except OSError:
            os.environ.pop(var, None)
            logger.warning("代理 %s=%s 连不上 -> 本进程已清空", var, val)


def get_help_message() -> str:
    return (
        "🤖 **中证1000 交易助手**\n\n"
        "直接把话发给我就行 —— 我是跑在你电脑上的 OpenCode agent。\n\n"
        "**行情 / 点位**\n"
        "- `现在多少点` `今天涨跌多少` —— 秒回\n"
        "- `报告` `大盘怎么看` —— 完整市场报告\n"
        "- `现在做多点位是多少` —— 实时点位（方向/入场/止损/止盈）\n"
        "- `我多单8100，止盈止损多少` —— 持仓管理\n"
        "- `明天计划做空的点位` —— 未来计划\n\n"
        "**通用能力**\n"
        "改代码、跑命令、查资料、生成文档；**直接发图片或文件**我也能看。\n"
        "需要我发文件给你时说一声，我会直接推送到钉钉（PDF/Excel/日志/代码都行）。\n\n"
        "**指令**\n"
        "`/new`（或 `/clear`）清空上下文开新会话　`/sid` 看会话状态\n"
        "`/model` 切换模型（点选即可：MiniMax 3.1 / DeepSeek 4.1 Flash / GLM 5.3 Flash）\n"
        "`/stop` 打断正在跑的任务　`/compact` 压缩上下文　"
        "`/fork` 复制一份上下文另开　`/restart` 重启机器人\n"
        "`/status` 看在线状态 / 最近一次停机　`/jobs` 看定时作业跑没跑\n"
        "`/run <作业> [参数]` 手动跑一次（快捷 `/ic` `/早报` `/周报`）\n"
        "`/help` 帮助\n\n"
        "> 机器人每次重启也会自动开新会话；同一时间最多并发 "
        f"{MAX_CONCURRENT_TURNS} 个任务。"
    )


class BridgeHandler(ChatbotHandler):
    def __init__(self, log=None):
        super().__init__()
        self.log = log or logger
        self._rate_hits: dict[str, list] = {}
        self._rate_lock = threading.Lock()
        # 排空标记：重启请求已排入时置 True，期间不再接新任务，
        # 等在飞回合跑完再退出（协同重启）。
        self.draining = False
        # 最近一条消息（供卡片「重试」按钮重放用；卡片回调里没有 msg 对象）
        self.last_msg: dict = {}

    def _allow_rate(self, staff: str) -> bool:
        """单人限速：滑动窗口内超过 RATE_LIMIT_MAX 条就拒收。"""
        now = time.time()
        with self._rate_lock:
            hits = [t for t in self._rate_hits.get(staff, [])
                    if now - t < RATE_LIMIT_WINDOW]
            if len(hits) >= RATE_LIMIT_MAX:
                self._rate_hits[staff] = hits
                return False
            hits.append(now)
            self._rate_hits[staff] = hits
        return True

    def _agent_turn(self, msg: ChatbotMessage) -> AgentTurn:
        return AgentTurn(self, msg, work_dir=OPENCODE_WORK_DIR,
                         data_dir=BRIDGE_DATA_DIR)

    def _run_agent(self, msg: ChatbotMessage, text: str, images: list,
                   files: list | None = None, quote: dict | None = None):
        """后台线程：把消息交给 OpenCode agent（含图片、文件附件、引用上下文）。"""
        try:
            self._agent_turn(msg).execute(text, images, files, quote)
        except Exception as e:  # noqa: BLE001
            self.log.exception("agent 处理异常: %s", e)
        finally:
            TURN_GATE.release(msg.conversation_id)

    # ---- 附件（文件/音频/视频）----
    _FILE_MSGTYPES = ("file", "audio", "video", "media")

    def _extract_file_specs(self, data: dict) -> list:
        """从回调原始数据里取出文件类附件的 downloadCode。

        SDK 的 ChatbotMessage 只解析 text/picture/richText，文件类消息会落进
        `extensions`，所以这里直接看原始 dict。
        """
        specs = []
        msgtype = (data or {}).get("msgtype") or ""
        content = (data or {}).get("content") or {}
        items = []
        if msgtype in self._FILE_MSGTYPES and isinstance(content, dict):
            items = [content]
        elif msgtype == "richText":
            items = [i for i in (content.get("content") or []) if isinstance(i, dict)]
        for it in items:
            code = it.get("downloadCode")
            if not code:
                continue
            specs.append({
                "download_code": code,
                "kind": it.get("type") or msgtype or "file",
                "fileName": it.get("fileName") or it.get("title") or f"{code}",
                "fileType": it.get("fileType") or "",
                "picURL": it.get("picURL") or "",
            })
        return specs

    async def process(self, callback):
        msg = ChatbotMessage.from_dict(callback.data)
        text = (msg.text.content.strip() if msg.text else "")
        staff = msg.sender_staff_id
        try:
            images = msg.get_image_list() or []
        except Exception:  # noqa: BLE001
            images = []
        # 文件/音频/视频/富文本里的附件（钉钉用 downloadCode 下发）
        files = self._extract_file_specs(callback.data)
        # 引用回复：钉钉把被引用内容塞在 text.repliedMsg 里（SDK 不解析）
        quote = _extract_quote(callback.data)
        self.log.info("收到消息: staff=%s type=%s text=%r images=%s files=%s quote=%s",
                      staff, getattr(msg, "message_type", "?"), text,
                      len(images), [f["fileName"] for f in files],
                      "无" if not quote else
                      (f"type={quote['msg_type']} msgId={quote['msg_id']} "
                       f"text={quote['text'][:200]!r} raw={json.dumps(quote['raw'], ensure_ascii=False)[:800]}"))

        _persist_recipient(msg)
        self.last_msg[msg.conversation_id] = msg
        if len(self.last_msg) > 20:                 # 有界，防止长跑膨胀
            for k in list(self.last_msg)[:10]:
                self.last_msg.pop(k, None)

        if staff not in ALLOWED_STAFF_IDS:
            self.log.warning("拒绝非白名单用户: %s", staff)
            self.reply_markdown("拒绝", "⛔ 未授权用户。", msg)
            return AckMessage.STATUS_OK, "OK"

        if self.draining:
            self.reply_markdown(
                "重启中",
                "🔄 机器人正在重启，约 10 秒后再发。\n\n"
                "（重启会清空对话上下文，原设计如此）", msg)
            return AckMessage.STATUS_OK, "OK"

        # 刚重启/刚掉线恢复：先补一句状态，别让用户以为消息石沉大海。
        # 只在这个进程启动后 15 分钟内有效，且只发一次（见 bot_presence）。
        gap_note = bot_presence.consume_gap_notice()
        if gap_note:
            self.reply_markdown("状态", gap_note, msg)

        if not self._allow_rate(staff):
            self.reply_markdown(
                "限速",
                f"⏳ 发得太快了：{RATE_LIMIT_WINDOW:.0f} 秒内最多 "
                f"{RATE_LIMIT_MAX} 条。稍等几秒再发。", msg)
            return AckMessage.STATUS_OK, "OK"

        if not text and not images and not files and not quote:
            self.reply_markdown("提示", "请发送文本、图片或文件，发 `帮助` 看用法。", msg)
            return AckMessage.STATUS_OK, "OK"

        low = text.lower()

        if low in ("/help", "/帮助") or text == "帮助":
            self.reply_markdown("📖 使用指南", get_help_message(), msg)
            return AckMessage.STATUS_OK, "OK"

        if low in ("/status", "/状态"):
            self.reply_markdown("状态", bot_presence.status_line(), msg)
            return AckMessage.STATUS_OK, "OK"

        if low in ("/jobs", "/任务", "/作业"):
            # 定时作业的运行情况：今天跑没跑、成功没、下次什么时候
            try:
                from scheduler.daemon import status_text
                body = status_text()
            except Exception as e:  # noqa: BLE001
                body = f"读不到调度器状态：{e}"
            self.reply_markdown("定时作业", body, msg)
            return AckMessage.STATUS_OK, "OK"

        head = text.split()[0].lower() if text.split() else ""
        if head in ("/run", "/ic", "/早报", "/周报"):
            # 手动触发：和定时跑的是同一份作业定义，只是触发源不同
            self._manual_job(text, staff, msg)
            return AckMessage.STATUS_OK, "OK"

        if low == "/sid":
            turn = self._agent_turn(msg)
            sid = turn.sessions.get(msg.conversation_id) or "(尚未创建)"
            turns = turn.sessions.turns(msg.conversation_id)
            limit = ROTATE_TURNS
            rec = turn.sessions.data.get(msg.conversation_id) or {}
            hist = rec.get("history") or []
            extra = (f"\n已轮换 {len(hist)} 次"
                     + (f"，上一段 `{(hist[-1] or {}).get('sid')}`"
                        f"（{(hist[-1] or {}).get('turns')} 轮）" if hist else ""))
            self.reply_markdown(
                "会话",
                f"`{sid}`\n\n本会话已对话 **{turns}/{limit}** 轮"
                f"{extra}\n（到 {limit} 轮会自动换新会话，并带上交接摘要）",
                msg)
            return AckMessage.STATUS_OK, "OK"

        if low in ("/new", "/clear", "/清空", "/新会话"):
            turn = self._agent_turn(msg)
            sid = turn.session_id(force_new=True)
            self.reply_markdown(
                "会话",
                f"✅ 已清空上下文，开新会话\n`{sid}`\n\n"
                "（旧对话仍保留在 OpenCode 历史里，只是不再作为上下文）",
                msg)
            return AckMessage.STATUS_OK, "OK"

        if low.startswith("/model"):
            arg = text[len("/model"):].strip()
            turn = self._agent_turn(msg)
            sid = turn.session_id()
            try:
                cur = OpenCodeService().session_model(sid)
            except Exception as e:  # noqa: BLE001
                cur = None
                self.log.warning("读当前模型失败：%s", e)

            if not arg:
                # 默认走提问卡：3 个模型点选即切（手机端不用手打 id）
                if start_model_choice(msg, sid, current=model_name(cur),
                                      log=self.log):
                    return AckMessage.STATUS_OK, "OK"
                # 卡片不可用 → 文字列表兜底（回数字或完整 id 都行）
                lines = [f"当前：{model_name(cur)}", "", "选一个："]
                lines += [f"- `{i}` {c['label']}（`{c['id']}`）"
                          for i, c in enumerate(MODEL_CHOICES, 1)]
                lines += ["", "回 `/model 1` / `2` / `3`，或 `/model <模型id>`"]
                self.reply_markdown("模型", "\n".join(lines), msg)
                return AckMessage.STATUS_OK, "OK"

            try:
                line = switch_model_checked(sid, arg, log=self.log)
            except Exception as e:  # noqa: BLE001
                self.reply_markdown("模型", f"❌ {e}", msg)
            else:
                self.reply_markdown("模型", line, msg)
            return AckMessage.STATUS_OK, "OK"

        if low in ("/stop", "/打断", "/停止"):
            sid = self._agent_turn(msg).sessions.get(msg.conversation_id)
            if not sid:
                self.reply_markdown("停止", "当前没有在跑的会话。", msg)
                return AckMessage.STATUS_OK, "OK"
            try:
                hit = OpenCodeService().interrupt(sid)
            except Exception as e:  # noqa: BLE001
                self.reply_markdown("停止", f"❌ 中断失败：`{e}`", msg)
            else:
                self.reply_markdown(
                    "停止", "🛑 已请求中断，回答会停在当前进度。"
                    if hit else "当前没有正在执行的任务。", msg)
            return AckMessage.STATUS_OK, "OK"

        if low in ("/compact", "/压缩"):
            sid = self._agent_turn(msg).sessions.get(msg.conversation_id)
            if not sid:
                self.reply_markdown("压缩", "还没有会话，先聊一句吧。", msg)
                return AckMessage.STATUS_OK, "OK"
            try:
                OpenCodeService().compact(sid)
            except Exception as e:  # noqa: BLE001
                self.reply_markdown("压缩", f"❌ 压缩失败：`{e}`", msg)
            else:
                self.reply_markdown(
                    "压缩",
                    "✅ 已请求压缩上下文（在当前步骤结束时执行）。\n\n"
                    "内置压缩比手工交接摘要保留得多，聊长对话时偶尔用一次。",
                    msg)
            return AckMessage.STATUS_OK, "OK"

        if low in ("/fork", "/分叉"):
            turn = self._agent_turn(msg)
            sid = turn.sessions.get(msg.conversation_id)
            if not sid:
                self.reply_markdown("分叉", "还没有会话，先聊一句吧。", msg)
                return AckMessage.STATUS_OK, "OK"
            try:
                new = OpenCodeService().fork(sid)
            except Exception as e:  # noqa: BLE001
                self.reply_markdown("分叉", f"❌ 分叉失败：`{e}`", msg)
            else:
                turn.sessions.set(msg.conversation_id, new["id"])
                self.reply_markdown(
                    "分叉",
                    f"✅ 已把当前上下文复制成新会话并切过去\n`{new['id']}`\n\n"
                    "（旧会话仍保留，可在 OpenCode 里查看/继续）", msg)
            return AckMessage.STATUS_OK, "OK"

        if low in ("/restart", "/重启"):
            req = restart_request()
            if req:
                self.reply_markdown(
                    "重启",
                    f"🔄 已经有一次待重启请求了（{req.get('requested_at_str')}），"
                    "本轮跑完就会重启。", msg)
                return AckMessage.STATUS_OK, "OK"
            request_restart(reason=f"用户 {staff} 发送 /restart", by=staff)
            busy = TURN_GATE.busy_count
            self.reply_markdown(
                "重启",
                "🔄 已排入协同重启。\n\n"
                + (f"当前还有 {busy} 个任务在跑，等它们跑完再做。"
                   if busy else "当前没有任务在跑，马上执行。")
                + "\n\n机器人会在本轮回答发完、卡片收尾之后自行退出，"
                "由看门狗重新拉起（约 10 秒断线）。\n\n"
                "⚠️ 重启会清空对话上下文（原设计如此）。",
                msg)
            return AckMessage.STATUS_OK, "OK"

        # 其余一切（含图片/文件）→ OpenCode agent（并发闸门保护）
        ok, why = TURN_GATE.acquire(msg.conversation_id)
        if not ok:
            self.reply_markdown("⏳ 忙", why, msg)
            return AckMessage.STATUS_OK, "OK"
        try:
            threading.Thread(target=self._run_agent,
                             args=(msg, text, images, files, quote),
                             daemon=True).start()
        except Exception:
            TURN_GATE.release(msg.conversation_id)
            raise
        return AckMessage.STATUS_OK, "OK"


    # ---------------------------------------------------------- 手动跑作业
    def _manual_job(self, text: str, staff: str, msg) -> None:
        """`/run <作业> [参数]`：手动触发调度器作业（异步，跑完回推结果）。"""
        from scheduler import manual

        parts = text.split()
        head = parts[0].lower().lstrip("/")
        args = parts[1:]
        if head != "run":          # /ic、/早报、/周报 这类快捷名
            args = [head] + args

        if not args:
            self.reply_markdown(
                "手动跑作业",
                "用法：`/run <作业> [参数]`\n\n" + manual.jobs_text()
                + "\n\n例：`/run ic --dry-run`（只算不推）、"
                "`/run ic --force`（忽略当日去重重发）", msg)
            return

        job = manual.find_job(args[0])
        if job is None:
            self.reply_markdown(
                "手动跑作业",
                f"没有这个作业：`{args[0]}`\n\n" + manual.jobs_text(), msg)
            return

        extra = args[1:]
        res = manual.start_manual(
            job, extra_args=extra, actor=staff,
            on_done=lambda r: self._manual_done(job, r, staff))

        if res.get("status") == "busy":
            self.reply_markdown(
                "⚠ 正在跑",
                f"**{job.name}** 已经在跑了（{res.get('running_since')} 开始），"
                "本次没有重复启动。\n\n等它跑完再点，或 `/jobs` 看状态。", msg)
            return
        if not res.get("ok"):
            self.reply_markdown("❌ 未启动", res.get("reason") or "未知原因", msg)
            return

        note = f"（参数 `{' '.join(extra)}`）" if extra else ""
        self.reply_markdown(
            "▶ 已启动",
            f"**{job.name}** 已开始跑{note}\n\n跑完我把结果直接推给你，"
            "也可以 `/jobs` 看状态。", msg)

    def _manual_done(self, job, res: dict, staff: str | None) -> None:
        """手动作业跑完的回推（独立消息，不占当前卡片）。"""
        from scheduler import runner

        status = res.get("status", "?")
        icon = {"success": "✔", "failed": "✖", "timeout": "⏱"}.get(status, "•")
        lines = [f"{icon} **{job.name}** 手动跑完：{status}",
                 f"`{res.get('occurrence', '')}`"]
        if res.get("reason"):
            lines.append(f"原因：{res['reason']}")
        tail = (runner.tail_log(job, 6) or "").strip()
        if tail:
            short = [ln[:180] for ln in tail.splitlines()[-6:]]
            lines += ["", "日志尾巴：", *[f"`{ln}`" for ln in short if ln.strip()]]
        body = "\n".join(lines)
        try:
            from tools.dingtalk_push import push_markdown
            if not push_markdown(body, title=f"手动作业 {job.name}",
                                 to_staff_id=staff):
                self.log.warning("手动作业 %s 结果回推失败", job.id)
        except Exception as e:  # noqa: BLE001
            self.log.warning("手动作业 %s 结果回推异常：%s", job.id, e)

    def retry_turn(self, conv: str, text: str) -> bool:
        """卡片「重试」按钮的回调：用同一条提问重跑一轮。

        并发闸门同样生效——重试不能在旧回合还没跑完时硬塞第二个请求。
        """
        msg = self.last_msg.get(conv)
        if msg is None:
            self.log.warning("重试：找不到会话 %s 的上一条消息", conv)
            return False
        ok, why = TURN_GATE.acquire(conv)
        if not ok:
            self.reply_markdown("⏳ 忙", why, msg)
            return False
        try:
            threading.Thread(target=self._run_agent, args=(msg, text, [], None),
                             daemon=True).start()
        except Exception:
            TURN_GATE.release(conv)
            raise
        self.log.info("卡片重试已入队：conv=%s text=%r", conv, text[:40])
        return True


class CardCallbackHandler(CallbackHandler):
    """钉钉卡片按钮回调（topic `/v1.0/card/instances/callback`）。

    只做一件事：把回调原文交给桥接层的 dispatch_card_callback 去结算。
    注意**先 ACK 再干活**会更快，但这里的处理都是内存级的（置 Event / 起线程），
    所以直接同步处理即可，避免引入队列复杂度。
    """

    def __init__(self, log=None):
        super().__init__()
        self.log = log or logger

    async def process(self, callback):
        try:
            data = callback.data or {}
            dispatch_card_callback(data, log=self.log)
        except Exception as e:  # noqa: BLE001
            self.log.warning("卡片回调处理异常：%s", e)
        return AckMessage.STATUS_OK, "OK"


# ============================================================ 单实例保护
_SINGLETON_MUTEX = "Local\\dingtalk_bot_v3"
_mutex_handle = None      # 必须保持引用：句柄被回收 → 互斥体释放 → 保护失效
PID_FILE = os.path.join(HERE, "logs", "dingtalk_v3.pid")


def acquire_single_instance(log) -> bool:
    """确保同一时刻只有一个机器人实例在跑。

    钉钉 Stream 模式**同一 APP_KEY 只能有一个连接**（官方：多连接时钉钉会随机挑一个
    通道推送，消息会丢/错乱）。这里用 Windows 命名互斥体做保护——进程退出（含崩溃）
    时由内核自动释放，不存在"陈旧锁文件"的问题。

    同时把 PID 写到 logs/dingtalk_v3.pid，供外部脚本（开机自启）快速判断。
    返回 True 表示可以继续启动。
    """
    global _mutex_handle
    try:
        import ctypes
        ERROR_ALREADY_EXISTS = 183
        _mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, False,
                                                           _SINGLETON_MUTEX)
        if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            log.error("❌ 已有机器人实例在运行（单实例保护），本次启动退出。"
                      "如需重启，请先结束旧进程。")
            return False
    except Exception as e:  # noqa: BLE001
        log.warning("单实例保护不可用（%s），继续启动", e)

    try:
        os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
        with open(PID_FILE, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        atexit.register(lambda: os.path.exists(PID_FILE) and os.remove(PID_FILE))
    except Exception as e:  # noqa: BLE001
        log.warning("写 PID 文件失败: %s", e)
    return True


# ==================================================== 协同重启（安全点退出）
# 重启请求由本进程在「安全点」执行：拒收新任务 → 等在飞回合排空 →
# 拉起替换进程 → 自己退出。这样正在渲染的卡片能正常收尾，不会变僵尸卡。
REPLACER_SCRIPT = os.path.join(os.path.expanduser("~"), "bin",
                               "dingtalk-bot-autostart.ps1")


def _spawn_replacer(log):
    """拉起一个「睡 3 秒再跑 autostart」的游离进程。

    不能退出前直接 start：单实例命名互斥体还握在本进程手里，新进程会在
    acquire_single_instance 处直接退出。等真正退出后再拉起才安全
    （autostart 本身幂等，看到已存活就什么都不做）。
    游离 + 输出重定向：避免它的 stdout/stderr 继承调用方管道导致挂死。
    """
    if os.name != "nt":
        return
    try:
        cmd = f"Start-Sleep -Seconds 3; & '{REPLACER_SCRIPT}'"
        flags = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED|NEW_GROUP|NO_WINDOW
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", cmd],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        log.info("协同重启：已拉起替换进程（3 秒后执行 autostart）")
    except Exception as e:  # noqa: BLE001
        log.warning("协同重启：拉起替换进程失败（看门狗会在 1 分钟内兜底）：%s", e)


def start_restart_watcher(handler, log):
    """后台线程：看到重启标记 → 置排空 → 等回合跑完 → 换进程 → 退出。"""

    def _loop():
        drain_deadline = 0.0
        while True:
            time.sleep(2.0)
            if not restart_pending():
                continue
            if not handler.draining:
                req = restart_request()
                handler.draining = True
                drain_deadline = time.time() + 900
                log.info("收到重启请求（%s）：拒收新任务，等 %d 个回合跑完",
                         req.get("reason") or "-", TURN_GATE.busy_count)
                # 先跟用户打招呼再断线：否则停机那 10 秒发来的消息石沉大海
                try:
                    bot_presence.push_restart_notice(req.get("reason") or "")
                except Exception as e:  # noqa: BLE001
                    log.warning("推送重启提示失败（不影响重启）：%s", e)
            if TURN_GATE.busy_count > 0 and time.time() < drain_deadline:
                continue
            # 多等一拍，确保最后一张卡片的收尾/投递已经发出去
            time.sleep(3.0)
            log.info("协同重启：任务已排空（busy=%d），换进程并退出",
                     TURN_GATE.busy_count)
            try:
                bot_presence.mark_down(restart_request().get("reason") or "计划内重启")
            except Exception as e:  # noqa: BLE001
                log.warning("记录停机起点失败：%s", e)
            try:
                if os.path.exists(PID_FILE):
                    os.remove(PID_FILE)
            except OSError:
                pass
            _spawn_replacer(log)
            time.sleep(1.0)
            os._exit(0)

    threading.Thread(target=_loop, daemon=True, name="restart-watcher").start()
    log.info("已就绪：/restart 或 tools/bot_restart.py 可触发协同重启")


def main():
    log = setup_logger()
    _disable_dead_proxy()

    if not acquire_single_instance(log):
        return 0

    if not APP_KEY or not APP_SECRET:
        log.error("❌ 环境变量 APP_KEY 或 APP_SECRET 未设置！")
        return 1

    try:
        info = OpenCodeService().info()
        log.info("OpenCode 服务可用: version=%s pid=%s", info.get("version"),
                 info.get("pid"))
    except Exception as e:  # noqa: BLE001
        log.error("❌ 连不上本机 OpenCode 服务（先跑 `opencode service start`）: %s", e)
        return 1

    # 每次启动都开新会话：清空「钉钉会话 -> OpenCode session」映射。
    # 旧 session 不删除（仍可在 OpenCode 历史里查看），只是不再作为上下文延续。
    oc = OpenCodeService()
    sess_map = SessionMap(os.path.join(BRIDGE_DATA_DIR, "sessions.json"))
    cleared = sess_map.reset()
    if cleared:
        log.info("启动即清空上下文：已作废 %d 个旧会话映射，下条消息将开新会话", cleared)
    # 盘前会话同理（每次开机重新积累一份干净的）
    pre_file = os.path.join(BRIDGE_DATA_DIR, "premarket_session.json")
    if os.path.exists(pre_file):
        try:
            os.remove(pre_file)
            log.info("盘前会话映射已清空")
        except OSError as e:
            log.warning("清空盘前会话映射失败: %s", e)

    client = DingTalkStreamClient(Credential(APP_KEY, APP_SECRET))
    handler = BridgeHandler(log)
    client.register_callback_handler(ChatbotMessage.TOPIC, handler)

    # 卡片回调（停止按钮 / 提问卡表单提交）：
    # 只要 card_v2 模块可用就注册——没开 v2 时它也是惰性的（v1 卡片不发回调），
    # 开着才能在真机上验证「按钮点了有没有回调」。
    if card_v2 is not None:
        set_retry_hook(handler.retry_turn)
        client.register_callback_handler(Card_Callback_Router_Topic,
                                         CardCallbackHandler(log))
        log.info("卡片回调已注册（stop / retry / ask%s）",
                 "，v2 已启用" if CARD_V2_OK else "，v2 未启用")
    if CARD_V2_OK:
        log.info("v2 卡片已启用：模板=%s，提问卡=%s",
                 os.getenv("DINGTALK_CARD_TEMPLATE_ID") or "公共模板 675cde2f",
                 "开" if getattr(card_v2, "ASK_CARD_ENABLED", False) else "关")
    else:
        log.info("v2 卡片未启用（设 DINGTALK_CARD_V2=1 打开），继续用 SDK 卡片")

    # 启动告知：先算「上一进程停了多久」，再开始心跳。
    # 顺序很重要——心跳会覆盖 last_alive，晚了就算不出真实停机时长。
    # 放在「启动找回」之前：那一步可能要跑几秒，用户更该先收到「已恢复」。
    was_restart = restart_pending()
    clear_restart()          # 本次已是重启后的新进程，清掉旧标记
    try:
        info = bot_presence.on_start(was_restart=was_restart)
        log.info("启动状态告知：中断约 %.0f 秒（%s）%s",
                 info.get("gap_seconds") or 0, info.get("reason"),
                 "，已推送「已恢复」" if info.get("notified") else "")
        bot_presence.heartbeat_start()
    except Exception as e:  # noqa: BLE001
        log.warning("启动状态告知失败（不影响启动）：%s", e)

    # 启动找回：把上一进程遗留的 loading 卡片收尾（补上真实回答或标注未完成）
    try:
        n = recover_inflight(client, log)
        if n:
            log.info("启动找回：共收尾 %d 张遗留卡片", n)
    except Exception as e:  # noqa: BLE001
        log.warning("启动找回失败（不影响启动）：%s", e)

    start_restart_watcher(handler, log)


    log.info("🤖 v3 启动（纯桥接）work_dir=%s", OPENCODE_WORK_DIR)
    try:
        client.start_forever()
    except KeyboardInterrupt:
        log.info("🛑 收到退出信号，正在关闭...")
    finally:
        log.info("✅ 进程退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
