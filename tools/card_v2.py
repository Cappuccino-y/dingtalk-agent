"""钉钉 AI 卡片 v2 —— 走「公共模板 + 卡片 OpenAPI」的实现。

为什么另起一个模块（而不是继续用 dingtalk_stream 的 AIMarkdownCardInstance）：
SDK 自带的 AI 卡片用的是公共模板 `382e4302-…-2e71.schema`，**只有两个变量**：
`msgContent`（正文）+ `staticMsgContent`（静态页脚）。一个正文 + 一个页脚，
就拿不到结构化的 block 列表、进度条、卡片内图片、按钮回调——天花板在这里。

钉钉开放平台其实有一张**公共 AI 卡片模板**，任何「企业内部机器人」都能直接引用，
不需要在卡片平台搭建：
    `675cde2f-f526-40cb-b828-f5b2b57b8b77.schema`（正文 + blockList + 引用 + 状态栏 + 按钮）
    `c2a6355b-9724-4f7e-9653-d33fcb3311bb.schema`（结构化提问卡）
本模块用后者这套模板，直接调卡片 OpenAPI（createAndDeliver / streaming / instances）。

模板变量约定（已核对模板导出 schema 与 mock 数据）：
    content       流式 markdown 正文（字符串）
    blockList     结构化块数组，**必须以 JSON 字符串形式传**（cardParamMap 只收字符串）
    quoteContent  卡片头部「引用原文」
    statusLine    卡片底部状态行（模型 / 耗时 / token …）
    copy_content  点「复制」时复制的内容（字符串）
    flowStatus    卡片状态："2" 输入中 / "3" 完成
    hasAction / stop_action  是否显示停止按钮

blockList 块类型（type）——**只有 0-3**，这是该公共模板 loopArray 的 schema 决定的
（对照 openclaw-channel-dingtalk 的 CardBlock 类型定义，四个类型就是全集）：
    0 = answer（markdown 正文，次级答案块）
    1 = think（思考/推理，次级灰色样式）
    2 = tool（工具执行，次级样式）
    3 = image（图片，需 mediaId）
**没有按钮块**：早期版本自己发明了 `type=4` 的按钮组，模板不认，形同虚设。
卡片上的「停止」按钮是模板**内置**的（由 hasAction / stop_action 控制，回调 actionId
是 `btn_stop`）；而「提问卡」走的是**另一张表单模板** `c2a6355b…`（见 ASK_CARD 部分）。

四条硬约束（都踩过）：
1. `cardParamMap` 的**每个值都必须是字符串**；传 int（例如 flowStatus: 2）会直接
   返回 400 `MissingStringValue`。所以状态码用字符串、blockList 用 json.dumps。
2. 想更新 blockList / quoteContent / statusLine 这些非流式变量，**必须先让卡片
   进入流式态**（发一次空的 content 流式帧），否则 IM 单聊场域下这些变量不生效。
3. 卡片模板与变量有客户端缓存：改了模板要重启钉钉客户端；换了模板 ID 或改了
   cardData，必须换成**全新的 outTrackId**，否则更新不生效。
4. **正文必须进 blockList，不能只放进 content**。核对该模板导出 schema 后的结论：
   公共模板的「完成」态（flowStatus=3，node `node_ocmncu094g7`）只渲染
   blockList + quoteContent + statusLine；`content` 组件挂在「输入中」态
   （flowStatus=2，node `node_ocmncu094g4`）里，完成态**不渲染它**。
   历史 bug：只把正文写进 content、blockList 留空 → 卡片终态一片空白。
5. **部分更新（只改 statusLine 之类）必须带 `cardUpdateOptions.updateCardDataByKey=true`**，
   否则是按 key 之外的字段被清掉——参考实现（openclaw）在所有 instances 更新里都带了它，
   历史「一会儿空白」的症状就是这里漏了。
"""
import json
import logging
import os
import re
import threading
import time
import uuid

import requests

logger = logging.getLogger("card_v2")

# ---------------------------------------------------------------- 常量

API_BASE = os.getenv("DINGTALK_API_BASE", "https://api.dingtalk.com")
MEDIA_UPLOAD = os.getenv("DINGTALK_MEDIA_UPLOAD",
                         "https://oapi.dingtalk.com/media/upload")

# 公共 AI 卡片模板（正文 + blockList + 引用 + 状态栏 + 按钮）
DEFAULT_TEMPLATE_ID = (os.getenv("DINGTALK_CARD_TEMPLATE_ID")
                       or "675cde2f-f526-40cb-b828-f5b2b57b8b77.schema")
# 公共结构化提问卡模板
ASK_USER_TEMPLATE_ID = (os.getenv("DINGTALK_ASK_USER_TEMPLATE_ID")
                        or "c2a6355b-9724-4f7e-9653-d33fcb3311bb.schema")

# 模板变量名（与模板 schema 对齐，改模板才需要动）
CONTENT_KEY = os.getenv("DINGTALK_CARD_CONTENT_KEY", "content")
BLOCK_LIST_KEY = os.getenv("DINGTALK_CARD_BLOCKLIST_KEY", "blockList")
COPY_CONTENT_KEY = os.getenv("DINGTALK_CARD_COPY_KEY", "copy_content")

# flowStatus：钉钉 AI 卡片内置状态
FLOW_INPUTING = "2"
FLOW_FINISHED = "3"

# 单块 markdown 的隐性长度上限：官方没写，社区实测约 3000 中文字符，
# 超限时**接口成功但客户端渲染成空白**，所以留出安全余量按码点分片。
BLOCK_CHAR_LIMIT = int(os.getenv("DINGTALK_BLOCK_CHARS", "2500"))
# 普通 markdown 消息（非卡片）的分片阈值，实测余量更大
MESSAGE_CHAR_LIMIT = int(os.getenv("DINGTALK_MESSAGE_CHARS", "3800"))
# 卡片正文总量上限（钉钉建议 ≤10KB）
CARD_MAX_BYTES = int(os.getenv("DINGTALK_CARD_MAX_BYTES", str(10 * 1024)))

# 是否启用 v2 卡片链路（环境变量开关，方便一键回退到 SDK 卡片）
V2_ENABLED = (os.getenv("DINGTALK_CARD_V2", "0") or "0").strip() == "1"

# 结构化提问卡（表单模板 c2a6355b…）是否启用。
# **默认关**：它需要真机点一次「选项 + 提交」才能确认回调链路，没验证前不开，
# 关着时 question 表单走老的「代答 + 提示改用文字」快路径（不会干等）。
ASK_CARD_ENABLED = (os.getenv("DINGTALK_ASK_CARD", "0") or "0").strip() == "1"
# 提问卡等用户点选的超时（秒）；超时即回退文字提问。
ASK_CARD_TIMEOUT = float(os.getenv("DINGTALK_ASK_CARD_TIMEOUT", "180") or "180")

# 卡片接口的 QPS 限制（官方连接器实测：不节流会回 403 QpsLimit）。
# 多会话叠加更容易踩到，所以令牌桶必须是**进程级共享**的，不能挂在单张卡片上。
CARD_QPS = int(os.getenv("DINGTALK_CARD_QPS", "20") or "20")
CARD_RETRY_DELAY = float(os.getenv("DINGTALK_CARD_RETRY_DELAY", "2.0") or "2.0")


# ---------------------------------------------------------------- 工具名归一化
# 进度文本只由归一化后的工具名派生，**不渲染命令参数、输出、URL、凭证**。
_TOOL_KINDS = (
    (("read", "view", "find", "list", "glob"), "检查文件"),
    (("write", "edit", "patch", "apply_patch"), "应用修改"),
    (("web_search", "websearch", "search", "fetch", "open", "open_url"), "查询资料"),
    (("browser",), "验证页面"),
    (("bash", "exec", "process", "shell", "command"), "执行检查"),
    (("database", "sql", "query"), "查询数据"),
)


def tool_kind(name: str) -> str:
    """把工具名映射成 7 类「任务种类」之一（不泄露参数）。"""
    low = (name or "").strip().lower()
    for keys, label in _TOOL_KINDS:
        for k in keys:
            if k in low:
                return label
    return "处理任务"


def tool_label(name: str) -> str:
    """工具名的短标签（只用于「当前在干嘛」，**绝不带参数/路径/命令**）。

    未登记的第三方工具（MCP 之类）以前一律落到「处理任务」，卡片上就等于
    什么都没说。这里退一步用工具名本身：去掉 mcp__ 前缀和路径式前缀，
    截到 12 字，安全且能说明在干什么。
    """
    raw = (name or "").strip()
    if not raw:
        return ""
    label = tool_kind(raw)
    if label != "处理任务":
        return label
    short = raw.split("__")[-1] or raw
    short = short.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem = short.rsplit(".", 1)
    if len(stem) == 2 and stem[0]:
        short = stem[0]
    return short[:12]


# ---------------------------------------------------------------- 「在干什么」
# 进度块以前只有一句「正在思考（已完成 3 步）」——用户的原话是「太 low，看不到
# 具体在干什么」。问题是那行字只由工具**种类**拼出来，而种类只有 7 个，翻来覆去
# 就那么几句。OpenCode 的 tool part 里其实带着 state.input（路径、pattern、命令）
# 和 time（起止毫秒），足够拼出「读取 bridge.py · 1.2s」这种具体的动作行。
#
# 安全边界（硬性）：**绝不上卡**命令原文、脚本正文、SQL、完整路径、URL 参数。
# 只取 basename / 关键词，截到 16 字；能说明在干什么就够了。
_FILE_KEYS = ("filePath", "file_path", "path", "filename", "file")
_QUERY_KEYS = ("pattern", "query", "q", "keyword", "search")
_SHELL_KEYS = ("bash", "shell", "exec", "cmd", "terminal", "process")
_EDIT_KEYS = ("write", "edit", "patch", "apply")


def _short(s, limit=16):
    s = strip_emoji_head(str(s or "")).strip().strip("`'\"")
    s = re.sub(r"\s+", " ", s)
    return (s[:limit - 1] + "…") if len(s) > limit else s


def _basename(p):
    p = str(p or "").strip().strip("`'\"")
    if not p:
        return ""
    return p.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or p


def _first_token(cmd):
    """命令的「程序名」。**只取这一个词**，参数一律不碰。

    `cd x; python a.py` 这种复合命令，第一个词是 cd（毫无信息量），所以跳过
    内置命令，取第一段真正在跑的程序。
    """
    cmd = str(cmd or "").strip()
    if not cmd:
        return ""
    for seg in re.split(r"(?:;|&&|\|\||\n)+", cmd):
        seg = seg.strip()
        if not seg:
            continue
        tok = re.split(r"[\s;&|<>()]+", seg)[0]
        low = tok.lower()
        if low in ("cd", "pushd", "export", "set", "source", "sudo", "time",
                   "then", "do", "env", "echo"):
            continue
        return _basename(tok)
    return ""


def _pick(inp, keys):
    for k in keys:
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def tool_action(part, limit=16):
    """tool part → 一句人话（卡片上「正在干什么」）。"""
    if not isinstance(part, dict):
        return ""
    name = str(part.get("name") or part.get("tool") or "")
    low = name.lower()
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    inp = state.get("input") if isinstance(state.get("input"), dict) else {}

    if any(k in low for k in _SHELL_KEYS):
        prog = _first_token(inp.get("command") or inp.get("cmd") or "")
        return f"执行 {prog}" if prog else "执行命令"
    if any(k in low for k in ("todo", "plan")):
        return "更新任务清单"
    if any(k in low for k in _EDIT_KEYS):
        f = _short(_basename(_pick(inp, _FILE_KEYS)), limit + 4)
        return f"{'写入' if 'write' in low else '修改'} {f}" if f else "改动文件"
    if any(k in low for k in ("read", "view", "cat")):
        f = _short(_basename(_pick(inp, _FILE_KEYS)), limit + 4)
        return f"读取 {f}" if f else "读取文件"
    if any(k in low for k in ("glob", "grep", "find", "search", "list", "ls")):
        q = _short(_pick(inp, _QUERY_KEYS) or _pick(inp, _FILE_KEYS), limit)
        return f"查找 {q}" if q else "查找文件"
    if any(k in low for k in ("webfetch", "fetch", "browser", "url", "http")):
        u = _pick(inp, ("url", "uri"))
        host = re.sub(r"^[a-zA-Z]+://", "", str(u or "")).split("/")[0]
        return f"打开 {_short(host, 20)}" if host else "打开网页"
    if any(k in low for k in ("task", "agent", "delegate")):
        return "派子任务"
    return tool_kind(name) if name else "处理任务"


def tool_state(part):
    st = (part.get("state") or {}) if isinstance(part, dict) else {}
    return str(st.get("status") or "")


def tool_secs(part, now=None):
    """工具真实耗时（秒）。OpenCode 的 part.time 是**毫秒时间戳**。

    **还在跑**的 part 没有 `completed`，以前一律返回 0 —— 结果一条跑三分钟的
    bash 在卡片上永远只是「▶ 执行命令」，看着像卡死。这里对 running 态按
    「现在 - 开始」估算，让活动行能挂上自己的计时（见 activity_header 的 step_secs）。
    """
    t = part.get("time") if isinstance(part, dict) else None
    if not isinstance(t, dict):
        return 0.0
    a = t.get("created") or t.get("ran")
    b = t.get("completed")
    if a and not b and tool_state(part) == "running":
        b = (now if now is not None else time.time()) * 1000.0
    try:
        if a and b and b >= a:
            return (b - a) / 1000.0
    except TypeError:
        pass
    return 0.0


def fmt_secs(sec):
    sec = max(0.0, float(sec or 0))
    if sec < 1:
        return "<1s"
    if sec < 10:
        return f"{sec:.1f}s"
    return f"{int(round(sec))}s"


# ---------------------------------------------------------------- 视觉 token
# 来源：钉钉官方《Markdown 变量》文档
#   https://open.dingtalk.com/document/development/markdown-variable-new
# 语法：`<font sizeToken=... colorTokenV2=...>文本</font>`（属性值**不加引号**）
#
# 这是把「过程」做成次级信息的官方手段。以前我们用 `> 引用` 硬凑层级：一条灰条
# 粗、字大、还带竖线，四行叠起来就像四个告示牌——用户原话「太 low，不美观」。
# 现在改成「小字号 + 灰阶」：过程行 12px 灰，当前动作 15px 蓝，视觉上让位给正文。
#
# 手机端实际字号：footnote 12px / h5 15px / body 17px（桌面端不同，不用管）。
FS_FOOTNOTE = "common_footnote_text_style__font_size"
FS_H5 = "common_h5_text_style__font_size"

C_PRIMARY = "common_level1_base_color"      # 正文主色
C_SECONDARY = "common_level2_base_color"    # 次级
C_TERTIARY = "common_level3_base_color"     # 三级（最淡）
C_BLUE = "common_blue1_color"
C_GREEN = "common_green1_color"
C_ORANGE = "common_orange1_color"
C_RED = "common_red1_color"
C_GRAY3 = "common_gray3_color"


def _safe_text(t):
    """markdown 变量里的字体标签用尖括号，正文里的尖括号必须去掉，否则标签被打断。"""
    return re.sub(r"[<>]", "", str(t or "")).strip()


# 字体 token 开关：万一某个客户端版本不认 <font ...>（会把标签当正文吐出来），
# 设 DINGTALK_CARD_FONT_TOKENS=0 立刻退回纯文本，不用改代码。
FONT_TOKENS = (os.getenv("DINGTALK_CARD_FONT_TOKENS", "1") or "1").strip().lower() \
    not in ("0", "false", "off", "no")


def font(text, size=FS_FOOTNOTE, color=C_GRAY3):
    """包一层官方字体标签（小字号 / 灰阶）。空文本返回空串。"""
    t = _safe_text(text)
    if not t:
        return ""
    if not FONT_TOKENS:
        return t
    return f"<font sizeToken={size} colorTokenV2={color}>{t}</font>"


def block_activity(markdown, plain="", tool=False):
    """过程行 block（type 1 思考 / type 2 工具，模板都按次级样式渲染）。"""
    md = (markdown or "").strip()
    if not md:
        return None
    return {"type": 2 if tool else 1, "markdown": md,
            "text": _safe_text(plain) or _safe_text(md), "mediaId": ""}


def activity_header(action="", done=0, thinking=False, silent=None,
                    step_secs=None, writing=False):
    """「正在干什么」那一行：当前动作 15px 蓝，步数/单步耗时 12px 淡灰。

    单个 block 内放两个 font 标签是允许的（同一行内联），这样能做出
    「主信息 + 弱化补充」的层次，而不是一整行同权重的大字。

    step_secs：**当前这一工具**已经跑了多久。只在跑得久时由调用方传入
    （短步骤不写，免得和底部总耗时抢注意力）；一旦出现，底部状态行就不再
    重复总计时，全卡只留一个「钟」。
    """
    if action:
        icon, text, color = "▶", action, C_BLUE
    elif silent is not None and silent >= 0:
        icon, text, color = "⏳", f"已静默 {int(silent)}s", C_ORANGE
    elif writing:
        icon, text, color = "✍", "正在写回答", C_SECONDARY
    elif thinking:
        icon, text, color = "💭", "正在思考", C_SECONDARY
    else:
        icon, text, color = "⚙", "正在处理", C_SECONDARY
    head = font(f"{icon} {text}", size=FS_H5, color=color)
    if step_secs and step_secs >= 1:
        head += font(f"　{_fmt_dur(step_secs)}", size=FS_FOOTNOTE,
                     color=C_TERTIARY)
    if done:
        # 「步」对用户是个黑话：里面的步数还混着思考步，用户根本不知道一步是什么。
        # 只说真实发生过的工具调用次数，口径和 trail 里的 ✓ 行一致。
        head += font(f"　{done} 个工具调用", size=FS_FOOTNOTE, color=C_TERTIARY)
    return head


def activity_step(part, limit=16, detail=True):
    """一条已完成/失败的步骤：`✓ 读取 bridge.py · 1.2s`（12px 灰）。"""
    if detail:
        act = tool_action(part, limit)
    else:
        act = tool_label(part.get("name") or "") if isinstance(part, dict) else ""
    if not act:
        return ""
    failed = tool_state(part) in ("error", "failed")
    mark = "✗" if failed else "✓"
    secs = tool_secs(part)
    body = f"{mark} {act} · {fmt_secs(secs)}" if secs else f"{mark} {act}"
    return font(body, size=FS_FOOTNOTE,
                color=C_RED if failed else C_GRAY3)


def block_thought(text, limit=60):
    """模型最新一句思考（灰字 12px，单行 block）。

    思考是连续吐出来的一大段：整段塞进卡片既超长又没重点，所以只取**最新
    一句**（按句号/问号/换行切），超长再截尾 —— 用户看到的是「现在在想什么」，
    而不是一份思维流水账。reasoning 里可能混着 markdown 记号，这里一并压掉。
    """
    t = " ".join(str(text or "").split())
    t = t.replace("`", "").replace("**", "").replace("__", "").strip()
    if not t:
        return None
    segs = [x for x in re.split(r"(?<=[。！？!?；;])", t) if x.strip()]
    t = (segs[-1] if segs else t).strip()
    if len(t) > limit:
        t = "…" + t[-(limit - 1):]
    body = font(f"💭 {t}", size=FS_FOOTNOTE, color=C_TERTIARY)
    return block_activity(body, plain=f"💭 {t}")


def progress_line(action="", done=0, thinking=False, silent=None,
                  step_secs=None):
    """纯文本版的「正在干什么」（不带字体标签，日志/降级用）。"""
    if silent is not None and silent >= 0:
        head = f"⏳ 已静默 {int(silent)}s"
    elif action:
        head = f"▶ {action}"
    elif thinking:
        head = "💭 正在思考"
    else:
        head = "⚙ 正在处理"
    if step_secs and step_secs >= 1:
        head += f" · {_fmt_dur(step_secs)}"
    return f"{head} · {done} 个工具调用" if done else head


def trail_line(part, limit=16):
    """纯文本版的步骤行（不带字体标签）。"""
    act = tool_action(part, limit)
    if not act:
        return ""
    mark = "✗" if tool_state(part) in ("error", "failed") else "✓"
    secs = tool_secs(part)
    return f"{mark} {act} · {fmt_secs(secs)}" if secs else f"{mark} {act}"


def _fmt_dur(sec):
    sec = max(0, int(sec or 0))
    if sec < 60:
        return f"{sec} 秒"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m} 分 {s} 秒" if s else f"{m} 分"
    h, m = divmod(m, 60)
    return f"{h} 小时 {m} 分"


def status_line(model=None, elapsed=None, tokens=None, agent=None, extra=None):
    """卡片底部状态行。只拼有值的项，保持一行。

    耗时 < 1 秒不显示：刚建卡就写「0 秒」纯属噪声（真机看着像卡住了）。
    """
    bits = []
    if model:
        bits.append(str(model))
    if elapsed is not None and elapsed >= 1:
        bits.append(_fmt_dur(elapsed))
    if tokens:
        bits.append(f"{tokens} tokens")
    if agent:
        bits.append(str(agent))
    if extra:
        bits.append(str(extra))
    return " | ".join(bits)


_EMOJI_HEAD = re.compile(r"^[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\s]+")


def strip_emoji_head(text):
    """去掉行首 emoji（钉钉是单色小图标，一串 emoji 更乱不更好读）。"""
    return _EMOJI_HEAD.sub("", str(text or "")).strip()


# ---------------------------------------------------------------- 块构造

def block_answer(markdown):
    return {"type": 0, "markdown": markdown or "", "text": "", "mediaId": ""}


def block_think(text):
    """思考块：次级样式（模板对 type<3 的块走 markdown 渲染，这里用引用块做视觉降级）。"""
    t = (text or "").strip()
    if not t:
        return None
    return {"type": 1, "markdown": "> " + t.replace("\n", "\n> "), "text": t,
            "mediaId": ""}


def block_tool(text):
    """工具块：同样次级样式；只放归一化后的短文本，不放原始输出。"""
    t = (text or "").strip()
    if not t:
        return None
    return {"type": 2, "markdown": "> " + t.replace("\n", "\n> "), "text": t,
            "mediaId": ""}


def block_image(media_id, text=""):
    if not (media_id or "").strip():
        return None
    return {"type": 3, "mediaId": media_id, "text": text or "", "markdown": ""}


# 注意：**不要**再造 type=4 的按钮块。该公共模板的 blockList schema 只认 0-3
# （answer/think/tool/image），塞进去的按钮不会被渲染。卡片上的「停止」是模板
# 内置按钮（hasAction / stop_action 控制）；「提问」是另一张表单模板。


def blocks_to_json(blocks):
    """blockList 必须以 JSON 字符串传给 cardParamMap。"""
    clean = [b for b in (blocks or []) if b]
    return json.dumps(clean, ensure_ascii=False)


# ---------------------------------------------------------------- 长文分片

_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")


def split_markdown(text, limit=None):
    """把超长 markdown 按**码点**切成多片，供拆多 block / 拆多卡使用。

    要求：
    - 按码点切，不切坏 emoji / 代理对；
    - 优先在换行处断开；
    - 代码围栏跨片自动闭合再重开，避免半截围栏把后面内容全变成代码。

    返回：[(chunk, is_continuation), ...]，单段时返回一段。

    注意：**永远返回 (chunk, is_continuation) 元组**，不要对短文本返回裸字符串
    —— 早期版本就是这么写的，调用方 `chunks[0][0]` 拿到的是「第一个字符」，
    直接把卡片正文变成一个字（线上空白卡的成因之一）。
    """
    limit = int(limit or BLOCK_CHAR_LIMIT)
    text = text or ""
    if len(text) <= limit:
        return [(text, False)]

    lines = text.split("\n")
    chunks, cur, cur_len = [], [], 0
    fence = None               # 当前未闭合的围栏标记（``` / ~~~）

    def close_and_flush():
        """收尾当前片：围栏若未闭合先补一个闭合标记，否则整片会被当成代码。"""
        nonlocal cur, cur_len
        if not cur:
            return
        if fence:
            cur.append(fence)
        chunks.append("\n".join(cur))
        cur, cur_len = [], 0

    for line in lines:
        # 逐行累加；单行本身超长时再按码点硬切
        pieces = [line]
        if len(line) > limit:
            pieces = [line[i:i + limit] for i in range(0, len(line), limit)]

        for piece in pieces:
            m = _FENCE_RE.match(piece)
            add = len(piece) + (1 if cur else 0)
            if cur and cur_len + add > limit:
                reopening = fence
                close_and_flush()
                if reopening:                      # 续片开头重开围栏
                    cur.append(reopening)
                    cur_len = len(reopening) + 1
            cur.append(piece)
            cur_len += len(piece) + 1
            if m:
                token = m.group(1)
                if fence == token:
                    fence = None
                elif fence is None:
                    fence = token
    close_and_flush()
    return [(c, i > 0) for i, c in enumerate(chunks) if c]


def normalize_for_v2(text):
    """v2 公共模板支持完整 markdown（标题/列表/表格/代码块/分隔线），
    所以这里基本原样透传，只做几处安全处理：
    - 去掉可能被当成模板变量的 `{{ }}`；
    - 压掉 3 行以上空行。
    """
    t = text or ""
    t = re.sub(r"\{\{.*?\}\}", "", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


# ---------------------------------------------------------------- access_token

class _TokenCache:
    """进程内共享的 access_token（钉钉有效期约 2 小时，提前 5 分钟刷新）。"""

    _lock = threading.Lock()
    _token = ""
    _expire_at = 0.0
    _session = None

    @classmethod
    def session(cls):
        if cls._session is None:
            s = requests.Session()
            # 系统 HTTP_PROXY 常指向没启动的代理，会把钉钉请求全拖超时
            s.trust_env = False
            cls._session = s
        return cls._session

    @classmethod
    def get(cls, app_key=None, app_secret=None):
        now = time.time()
        with cls._lock:
            if cls._token and now < cls._expire_at:
                return cls._token
            key = app_key or os.getenv("APP_KEY")
            sec = app_secret or os.getenv("APP_SECRET")
            if not key or not sec:
                raise RuntimeError("缺少 APP_KEY / APP_SECRET，无法取钉钉 access_token")
            r = cls.session().post(
                f"{API_BASE}/v1.0/oauth2/accessToken",
                json={"appKey": key, "appSecret": sec}, timeout=30)
            r.raise_for_status()
            data = r.json()
            cls._token = data["accessToken"]
            expire = int(data.get("expireIn") or 7200)
            cls._expire_at = now + max(60, expire - 300)
            logger.debug("钉钉 access_token 已刷新，%ds 后过期", expire)
            return cls._token


# ---------------------------------------------------------------- 限流 / 退避
class _RateLimiter:
    """进程级令牌桶：卡片接口有 QPS 限制，超了会静默失败（卡片停在旧状态）。"""

    def __init__(self, qps=None):
        self.gap = 1.0 / max(1, int(qps or CARD_QPS))
        self._lock = threading.Lock()
        self._ts = 0.0

    def wait(self):
        with self._lock:
            now = time.time()
            wait = self._ts + self.gap - now
            if wait > 0:
                time.sleep(wait)
                now = time.time()
            self._ts = now


_LIMITER = _RateLimiter()


def _is_throttle(status_code, text):
    s = (text or "").lower()
    return (status_code in (403, 429) or "qps" in s or "ratelimit" in s
            or "rate limit" in s or "too many requests" in s or "限流" in s)


def _request(method, url, **kw):
    """带全局限流 + 限流退避重试的卡片请求；非 200 抛 RuntimeError。

    以前这里是裸 requests 调用：一旦撞上 QPS 限流就**静默失败**，卡片会
    停在旧状态（表现就是「一直输入中」），很难排查。现在照抄 v1 写入器的
    策略：800ms 级防抖之外再叠令牌桶 + 一次退避重试。
    """
    kw.setdefault("timeout", 30)
    last_exc = None
    for attempt in (0, 1):
        _LIMITER.wait()
        try:
            r = _TokenCache.session().request(method, url, **kw)
        except Exception as e:  # noqa: BLE001  网络抖动
            last_exc = e
            if attempt == 0:
                time.sleep(CARD_RETRY_DELAY)
                continue
            raise
        if r.status_code == 200:
            return r
        if attempt == 0 and _is_throttle(r.status_code, r.text):
            logger.warning("卡片接口被限流（HTTP %s），%.1fs 后重试",
                           r.status_code, CARD_RETRY_DELAY)
            time.sleep(CARD_RETRY_DELAY)
            continue
        raise RuntimeError(f"{method} {url} HTTP {r.status_code} {r.text[:300]}")
    raise last_exc if isinstance(last_exc, Exception) else RuntimeError("卡片请求失败")


# ---------------------------------------------------------------- 卡片客户端

class CardV2:
    """一张 AI 卡片 v2 实例的生命周期封装。

    典型用法：
        card = CardV2(msg, title="OpenCode")
        if card.create(quote="用户原话"):
            card.stream("正在思考…")
            card.set_blocks([block_answer("...")])
            card.finalize(content="最终回答", status="deepseek | 12 秒")
        else:
            # 降级：调用方自己发普通 markdown
            ...
    """

    def __init__(self, msg, title="OpenCode", template_id=None,
                 robot_code=None, app_key=None, app_secret=None):
        self.msg = msg
        self.title = title or "OpenCode"
        self.template_id = template_id or DEFAULT_TEMPLATE_ID
        self.out_track_id = "oc-" + uuid.uuid4().hex[:20]
        self.card_instance_id = None
        self.robot_code = (robot_code
                           or getattr(msg, "robot_code", None)
                           or os.getenv("ROBOT_CODE"))
        self._app_key = app_key
        self._app_secret = app_secret
        self._stream_open = False
        self._last_stream_ts = 0.0
        self._closed = False
        self._lock = threading.Lock()

    # ---- 基础 ----
    def _headers(self):
        return {"x-acs-dingtalk-access-token": _TokenCache.get(self._app_key,
                                                              self._app_secret),
                "Content-Type": "application/json"}

    @staticmethod
    def _normalize_body(body):
        """出口处兜底：`cardParamMap` 的**每个值都必须是字符串**。

        踩过的坑：把一个 list 塞进 content，接口只回一句 `MissingStringValue`，
        **不告诉你是哪个字段**，排查很费劲。所以在唯一的出口处统一归一化，
        把这类错误挡在本地。
        """
        try:
            pm = body.get("cardData", {}).get("cardParamMap")
            if isinstance(pm, dict):
                for k, v in list(pm.items()):
                    if v is None:
                        pm.pop(k, None)
                    elif not isinstance(v, str):
                        pm[k] = json.dumps(v, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass
        return body

    def _post(self, path, body):
        r = _request("POST", API_BASE + path, headers=self._headers(),
                     data=json.dumps(self._normalize_body(body)))
        return r.json()

    def _put(self, path, body):
        r = _request("PUT", API_BASE + path, headers=self._headers(),
                     data=json.dumps(self._normalize_body(body)))
        return r.json()

    def _open_space(self):
        """单聊 → IM_ROBOT.{staff_id}；群聊 → IM_GROUP.{conversation_id}。

        单聊用 staff_id、群聊用 conversation_id 是与 SDK 一致且实测可用的写法。
        """
        conv = str(getattr(self.msg, "conversation_id", "") or "")
        staff = str(getattr(self.msg, "sender_staff_id", "") or "")
        ctype = str(getattr(self.msg, "conversation_type", "") or "")
        if ctype == "2" or (conv and staff and conv.endswith(staff)):
            return f"dtv1.card//IM_GROUP.{conv}", True
        return f"dtv1.card//IM_ROBOT.{staff}", False

    # ---- 生命周期 ----
    def create(self, quote="", status=""):
        """创建并投放 AI 卡片。成功返回 True，失败返回 False（调用方降级）。"""
        params = {
            "config": json.dumps({"autoLayout": True, "enableForward": True}),
            CONTENT_KEY: "",
            COPY_CONTENT_KEY: "",
            "flowStatus": FLOW_INPUTING,
            # 停止按钮由 hasAction 控制；回调 actionId 是 btn_stop（模板内置）。
            "hasAction": "true",
            "stop_action": "true",
        }
        if quote:
            params["quoteContent"] = quote[:500]
        if status:
            params["statusLine"] = status
        return self.create_raw(params)

    def create_raw(self, params, kick_stream=True):
        """用给定的 cardParamMap 投放卡片（提问卡等其它模板共用这条路径）。"""
        try:
            space, is_group = self._open_space()
            body = {
                "cardTemplateId": self.template_id,
                "outTrackId": self.out_track_id,
                "cardData": {"cardParamMap": params},
                "callbackType": "STREAM",
                "imGroupOpenSpaceModel": {"supportForward": True},
                "imRobotOpenSpaceModel": {"supportForward": True},
                "openSpaceId": space,
                "userIdType": 1,
                "imGroupOpenDeliverModel": ({
                    "robotCode": self.robot_code,
                    "extension": {"dynamicSummary": "true"}} if is_group else None),
                "imRobotOpenDeliverModel": (None if is_group else {
                    "spaceType": "IM_ROBOT",
                    "robotCode": self.robot_code,
                    "extension": {"dynamicSummary": "true"}}),
            }
            body = {k: v for k, v in body.items() if v is not None}
            res = self._post("/v1.0/card/instances/createAndDeliver", body)
            ok = bool(res.get("result", {}).get("deliverResults"))
            self.card_instance_id = self.out_track_id
            logger.info("v2 卡片已投放 outTrackId=%s template=%s",
                        self.out_track_id, self.template_id)
            # 立刻踢进流式态：IM 场域下不先进流式态，blockList/quote/statusLine
            # 这些非流式变量不会生效。（提问卡模板没有流式变量，跳过这一步。）
            if kick_stream:
                try:
                    self._stream(CONTENT_KEY, "", False)
                    self._stream_open = True
                except Exception as e:  # noqa: BLE001
                    logger.warning("v2 卡片进入流式态失败（不致命）：%s", e)
            return ok
        except Exception as e:  # noqa: BLE001
            logger.warning("v2 卡片创建失败：%s", e)
            return False

    def _stream(self, key, content, finalize):
        return self._put("/v1.0/card/streaming", {
            "outTrackId": self.out_track_id,
            "guid": str(uuid.uuid4()),
            "key": key,
            "content": content or "",
            "isFull": True,
            "isFinalize": bool(finalize),
            "isError": False,
        })

    def stream(self, text, force=False, min_interval=0.8):
        """流式刷新正文（content 变量）。带最小间隔防抖，避免撞 QPS。

        返回 True 表示这一版**真的推上去了**（调用方靠它判断客户端当前
        显示到哪一版，终态化前据此估算还要等多久，见 bridge 的 _drain_stream）。
        """
        if self._closed:
            return False
        text = text or ""
        now = time.time()
        with self._lock:
            if not force and now - self._last_stream_ts < min_interval:
                return False
            self._last_stream_ts = now
        # 正文超过单块上限时只推第一片，剩下交给 blockList 承载
        head = text
        if len(head.encode("utf-8")) > CARD_MAX_BYTES:
            head = head.encode("utf-8")[:CARD_MAX_BYTES - 32].decode("utf-8", "ignore")
        try:
            self._stream(CONTENT_KEY, head, False)
            self._stream_open = True
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("v2 流式更新失败：%s", e)
            return False

    def update_vars(self, **vars_):
        """更新非流式变量（blockList / statusLine / quoteContent / copy_content）。

        cardParamMap 的值必须是字符串；非字符串在这里统一转成 JSON 字符串。
        必须带 updateCardDataByKey：否则这次没提到的变量会被一起清掉
        （只改状态行 → blockList 被清空 = 卡片忽然变空白）。
        """
        if self._closed:
            return
        params = {}
        for k, v in vars_.items():
            if v is None:
                continue
            params[k] = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        if not params:
            return
        try:
            self._put("/v1.0/card/instances",
                      {"outTrackId": self.out_track_id,
                       "cardData": {"cardParamMap": params},
                       "cardUpdateOptions": {"updateCardDataByKey": True},
                       "userIdType": 1})
        except Exception as e:  # noqa: BLE001
            logger.warning("v2 变量更新失败 %s：%s", list(params), e)

    def set_blocks(self, blocks, status=None):
        self.update_vars(**{BLOCK_LIST_KEY: blocks_to_json(blocks)},
                         **({"statusLine": status} if status else {}))

    def set_status(self, status):
        self.update_vars(statusLine=status or "")

    def finalize(self, content="", blocks=None, status="", failed=False,
                 extra_text=None):
        """终态化。

        顺序很关键：先关闭流式生命周期，再用 instances 接口一次性写
        blockList + content + copy_content + statusLine + flowStatus。
        """
        if self._closed:
            return False
        self._closed = True
        content = normalize_for_v2(content or "")

        # 1) 正文一律进 blockList 的 answer 块（**这是关键**）。
        #    实测/核对模板 schema：公共 v2 模板的「完成」态
        #    （flowStatus=3，node_ocmncu094g7）只渲染 blockList，
        #    `content` 组件挂在「输入中」态（flowStatus=2 的 node_ocmncu094g4）里，
        #    完成态根本不渲染它。所以只把正文塞进 content、blockList 留空
        #    → 卡片渲染出来是**一片空白**（这就是线上那张空白卡的根因）。
        #    参考实现（openclaw-channel-dingtalk）同样是把正文拆成 type=0 块。
        chunks = [(c, cont) for c, cont in split_markdown(content) if isinstance(c, str)]
        if not chunks:
            chunks = [("", False)]
        answer_blocks = [block_answer(c) for c, _ in chunks]
        head = chunks[0][0]
        all_blocks = answer_blocks + list(blocks or [])
        content_var = content if len(content) <= BLOCK_CHAR_LIMIT else head
        copy_var = content if len(content) <= BLOCK_CHAR_LIMIT else head

        # 2) 关掉流式生命周期（否则钉钉侧一直停在「输入中」）。
        #    v5：最后一帧**必须带上终稿正文**，不能发空串。发空串等于在翻终态前
        #    先把正文从流式变量里擦掉——客户端要么白一下，要么把这次归零当成
        #    一次跳变，紧接着终态 blockList 又把全文写回来，看起来就是「啪」地
        #    全出来。官方文档的收尾写法也是「正常一帧 + isFinalize=true」。
        try:
            if self._stream_open:
                self._stream(CONTENT_KEY, content_var, True)
        except Exception as e:  # noqa: BLE001
            logger.warning("关闭流式生命周期失败（继续终态化）：%s", e)

        # 3) 一次性写终态。
        #    content 在完成态不参与渲染（见上），只服务于「复制富文本」按钮，
        #    所以短回答给全量、超长回答仍只给首片，避免单块超限。
        #    copy_content 同理按上限收敛：它是**第二份全文**，长回答时会让整包
        #    体积翻倍（实测 9000 字回答的报文能到 38KB），收益低于风险。
        params = {
            BLOCK_LIST_KEY: blocks_to_json(all_blocks),
            CONTENT_KEY: content_var,
            COPY_CONTENT_KEY: copy_var,
            "flowStatus": FLOW_FINISHED,
            "hasAction": "false",
            "stop_action": "false",
        }
        if status:
            params["statusLine"] = status
        if extra_text:
            params[CONTENT_KEY] = ((content_var + "\n\n" + extra_text).strip()
                                   if content_var else extra_text)
        size = len(json.dumps(params, ensure_ascii=False).encode("utf-8"))
        if size > 32 * 1024:
            logger.warning("v2 终态报文偏大（%d 字节，正文 %d 字）",
                           size, len(content))
        try:
            self._put("/v1.0/card/instances",
                      {"outTrackId": self.out_track_id,
                       "cardData": {"cardParamMap": params},
                       "cardUpdateOptions": {"updateCardDataByKey": True},
                       "userIdType": 1})
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("v2 卡片终态化失败：%s", e)
            return False

    def fail(self, message="（出错了，没有拿到回答）", status="❌ 执行失败"):
        """失败态：保留失败信息。

        该模板**没有按钮块**，做不了「重试」按钮（早期版本塞了 type=4 按钮，
        模板不渲染，等于没有）。所以失败态只保证信息清楚，重试靠用户重发消息。
        """
        return self.finalize(content=message, status=status, failed=True)

    # ---- 图片 ----
    def upload_image(self, path):
        """上传图片换 mediaId（image 块需要）。

        钉钉卡片的多媒体变量**只认开放平台文件存储的 mediaId**，不能直接塞外链。
        """
        try:
            token = _TokenCache.get(self._app_key, self._app_secret)
            with open(path, "rb") as fh:
                r = _TokenCache.session().post(
                    MEDIA_UPLOAD,
                    params={"access_token": token, "type": "image"},
                    files={"media": (os.path.basename(path), fh, "image/png")},
                    timeout=60)
            r.raise_for_status()
            media_id = (r.json() or {}).get("media_id")
            if not media_id:
                logger.warning("上传图片未返回 media_id：%s", r.text[:200])
            return media_id
        except Exception as e:  # noqa: BLE001
            logger.warning("上传图片失败 %s：%s", path, e)
            return None


# ---------------------------------------------------------------- 提问卡（表单模板）
# 提问卡不是「AI 卡片里塞按钮」，而是**另一张模板** `c2a6355b…`：
# 变量是 question_id / question_title / question_desc / card_status /
# form_btn_text / selected_text / selected_values / form(JSON 字符串)，
# `form.fields[]` 描述表单域，用户选完点「提交」，回调里带 params.form 回来。
# （协议对照 openclaw-channel-dingtalk 的 ask-user-question.ts，字段名逐个核对过。）

# 表单域类型白名单（模板只认这些）
FORM_FIELD_TYPES = {
    "TEXT", "TEXT_ARRAY", "TEXT_AREA", "NUMBER", "SELECT", "MULTI_SELECT",
    "DATE", "TIME", "DATETIME", "CHECKBOX", "SWITCH", "CHECKBOX_GROUP",
    "MULTI_CHECKBOX_GROUP",
}


def ask_field(name, label, options, required=True, multi=False):
    """构造一个「单选/多选」表单域（选项式提问用）。

    options: [{"label","value"}] -> 模板要的 [{"value","text"}]（注意是 text 不是 label）
    """
    opts = []
    for o in options or []:
        if not isinstance(o, dict):
            continue
        val = o.get("value")
        if val is None:
            val = o.get("label")
        if val is None:
            continue
        opts.append({"value": str(val), "text": str(o.get("label") or val)})
    if not opts:
        return None
    return {"name": name, "label": label or name,
            "type": "MULTI_SELECT" if multi else "SELECT",
            "required": bool(required), "options": opts}


# 提问卡状态（模板用 card_status 做状态表达式）：
# 表单域和提交按钮的 visible 条件都是 `is_pending`，所以**改掉 card_status 表单就收起**；
# 配色也跟着走：pending=蓝 / processing=紫 / submitted=绿 / cancelled=橙 / expired=红。
ASK_STATUS_PENDING = "pending"
ASK_STATUS_PROCESSING = "processing"
ASK_STATUS_SUBMITTED = "submitted"
ASK_STATUS_CANCELLED = "cancelled"
ASK_STATUS_EXPIRED = "expired"

# 终态 -> (提交按钮文案)。按钮虽然被隐藏了，但改掉能兜住模板万一渲染出来的情况。
ASK_STATUS_BTN = {
    ASK_STATUS_SUBMITTED: "已提交",
    ASK_STATUS_CANCELLED: "已取消",
    ASK_STATUS_EXPIRED: "已失效",
}


def ask_card_params(question_id, title, desc, fields, submit_text="提交"):
    """提问卡（模板 c2a6355b）的 cardParamMap。所有值都是字符串。

    变量只有模板声明的那几个：question_id / question_title / question_desc /
    form_btn_text / card_status / form。**模板没有「答案回显」变量**（核对过
    openclaw 的模板导出件），所以选了什么只能借 question_desc 显示。
    """
    return {
        "question_id": question_id,
        "question_title": title or "需要你的确认",
        "question_desc": desc or title or "请选择后提交",
        "card_status": ASK_STATUS_PENDING,
        "form_btn_text": submit_text or "提交",
        "form": json.dumps({"fields": [f for f in (fields or []) if f]},
                           ensure_ascii=False),
    }


def _unwrap(value):
    """回调里的字段可能是 JSON 串再包一层，统一拆开成 dict/list。"""
    if isinstance(value, str):
        s = value.strip()
        if s[:1] in ("{", "["):
            try:
                return json.loads(s)
            except Exception:  # noqa: BLE001
                return value
    return value


def parse_ask_answer(params):
    """从卡片回调的 params 里取出表单答案 → {field_name: value}。

    回调形状（对照参考实现）：params.form = {"answer_0": {"index":0,"value":"v"},
    "answer_1": {"index":[0,2],"value":["a","b"]}}；
    取消则 params.user_cancel。取不到任何答案返回 None（调用方回退文字提问）。
    """
    p = _unwrap(params) if isinstance(params, (str, dict)) else {}
    if not isinstance(p, dict):
        return None
    if p.get("user_cancel"):
        logger.info("提问卡：用户取消了")
        return None
    form = _unwrap(p.get("form"))
    if not isinstance(form, dict) or not form:
        return None
    answer = {}
    for key, raw in form.items():
        val = _unwrap(raw)
        if isinstance(val, dict):
            val = val.get("value", val.get("index"))
        if isinstance(val, list):
            val = [(_unwrap(v).get("value") if isinstance(_unwrap(v), dict) else v)
                   for v in val]
        answer[str(key)] = val
    return answer or None


def format_ask_summary(fields, answers, limit=120):
    """把回调答案拼成一行人类可读的摘要。

    模板没有答案回显变量，终态卡片是「空表单 + 状态角标」，什么信息都不留；
    所以把「你选了什么」塞进 question_desc，卡片收起后还能看出投过什么票。
    values 优先按 option.value 映射成 option.text；模板偶尔只回 index 时按序号兜底。
    """
    if not answers:
        return ""
    parts = []
    for f in fields or []:
        name = str(f.get("name") or "")
        if name not in answers:
            continue
        opts = f.get("options") or []
        raw = answers[name]
        for v in (raw if isinstance(raw, list) else [raw]):
            if v is None or v == "":
                continue
            sval = str(v)
            text = next((o.get("text") for o in opts
                         if str(o.get("value")) == sval), None)
            if text is None and sval.lstrip("-").isdigit():   # 只回 index 的兜底
                idx = int(sval)
                text = opts[idx].get("text") if 0 <= idx < len(opts) else None
            parts.append(str(text or sval))
    return " / ".join(parts)[:limit]


def parse_card_action(callback_params):
    """从卡片回调里解析出 (action_id, params)；解析不了返回 (None, {})。

    钉钉卡片回传的形状**很不统一**（这是钉钉的老毛病），参考实现
    （openclaw-channel-dingtalk）扫的字段比我们早先猜的多得多。这里按同一顺序
    归一化，覆盖实测/参考里出现过的全部位置：
      * 顶层 `actionId` / `action`
      * `content` / `value`（**JSON 字符串**，需再解一层）
      * `cardPrivateData.actionIds[0]`（表单/结构化回传真正用的就是它）
      * `params` 可能在 cardPrivateData / content / value / 顶层
    """
    try:
        data = json.loads(callback_params) if isinstance(callback_params, str) \
            else (callback_params or {})
    except Exception:  # noqa: BLE001
        return None, {}
    if not isinstance(data, dict):
        return None, {}

    def as_dict(v):
        v = _unwrap(v)
        return v if isinstance(v, dict) else None

    content = as_dict(data.get("content"))
    value = as_dict(data.get("value"))
    private = (as_dict(data.get("cardPrivateData"))
               or (as_dict(content.get("cardPrivateData")) if content else None)
               or (as_dict(value.get("cardPrivateData")) if value else None))

    # params：按 privateData -> content -> value -> 顶层 顺序取第一个非空 dict
    params = {}
    for src in (private, content, value, data):
        if isinstance(src, dict):
            cand = as_dict(src.get("params"))
            if cand:
                params = dict(cand)
                break

    # actionId：优先 cardPrivateData.actionIds[0]（表单提交就是这个）
    action = None
    ids = private.get("actionIds") if isinstance(private, dict) else None
    if isinstance(ids, list) and ids and isinstance(ids[0], str) and ids[0].strip():
        action = ids[0].strip()

    if not action:
        for src in (data, content, value, private):
            if not isinstance(src, dict):
                continue
            cand = src.get("actionId") or src.get("action")
            inner = as_dict(cand)
            if inner:                       # actionId 本身又是一段 JSON
                action = inner.get("actionId") or inner.get("action")
                params = {**params, **(as_dict(inner.get("params")) or {})}
            elif isinstance(cand, str) and cand.strip():
                action = cand.strip()
            if action:
                break

    if not action:
        action = params.get("actionId") or params.get("action")
    if isinstance(action, str):
        action = action.strip() or None
    return action, (params if isinstance(params, dict) else {})


def extract_out_track_id(data):
    """回调里取 outTrackId（顶层取不到就进 content/value/cardPrivateData 里找）。

    卡片归属（outTrackId -> conversation）全靠它，取错了整条回调就作废。
    """
    if not isinstance(data, dict):
        return ""
    for src in (data, _unwrap(data.get("content")), _unwrap(data.get("value")),
                _unwrap(data.get("cardPrivateData"))):
        if isinstance(src, dict):
            tid = src.get("outTrackId") or src.get("cardInstanceId")
            if isinstance(tid, str) and tid.strip():
                return tid.strip()
    return ""


def finish_ask_card(out_track_id, status, summary="", app_key=None, app_secret=None):
    """**按 outTrackId 更新一张 ASK 卡片的状态**（不需要卡片对象）。

    场景：卡片是别的进程创建的（比如 scheduler 的 evolve 作业推的审批卡），
    当前进程没有那张卡片的对象，但更新接口只要 outTrackId 就行。

    status 用 ASK_STATUS_* 常量；summary 会写进 question_desc，
    让用户在卡面上直接看到「已批准 / 已驳回」而不是一直停在待提交。
    """
    if not out_track_id:
        return False
    btn = ASK_STATUS_BTN.get(status, "")
    params = {"card_status": status}
    if btn:
        params["form_btn_text"] = btn
    if summary:
        params["question_desc"] = summary
    body = {
        "outTrackId": out_track_id,
        "cardData": {"cardParamMap": params},
        "cardUpdateOptions": {"updateCardDataByKey": True},
        "userIdType": 1,
    }
    try:
        r = _request("PUT", API_BASE + "/v1.0/card/instances",
                     headers={"x-acs-dingtalk-access-token":
                              _TokenCache.get(app_key, app_secret),
                              "Content-Type": "application/json"},
                     data=json.dumps(CardV2._normalize_body(body)))
        return bool(r)
    except Exception as e:  # noqa: BLE001
        logger.warning("问句卡状态更新失败（%s）：%s", out_track_id, e)
        return False


class AskCard:
    """一张结构化提问卡（表单模板 `c2a6355b…`）。

    用法：
        card = AskCard(msg, question_id="ask_xxx", title="选方向", desc="...",
                       fields=[ask_field("q0", "明天怎么走", [{"label":"涨","value":"up"}])])
        if card.create():
            ... 等回调 ...
    """

    def __init__(self, msg, question_id, title="", desc="", fields=None,
                 submit_text="提交", robot_code=None):
        self.msg = msg
        self.template_id = ASK_USER_TEMPLATE_ID
        self.question_id = question_id
        self.fields = [f for f in (fields or []) if f]
        self.params = ask_card_params(question_id, title, desc, self.fields,
                                      submit_text)
        self._card = CardV2(msg, template_id=self.template_id,
                            robot_code=robot_code)

    @property
    def out_track_id(self):
        return self._card.out_track_id

    def create(self) -> bool:
        if not self.fields:
            return False
        # 提问卡模板没有流式变量，不要踢流式态
        return self._card.create_raw(self.params, kick_stream=False)

    def finish(self, status, summary=""):
        """把卡片切到终态：收起表单 + 换状态角标配色。

        为什么必须做这一步：模板里表单域和提交按钮的 visible 条件都是
        `is_pending`（card_status == 'pending'）。不主动改 card_status，
        用户点完「提交」卡片会一直停在可填状态——看着像没生效。
        summary 选填，借 question_desc 回显「你选了什么」。

        必须带 outTrackId + updateCardDataByKey（见 update_vars 的注释）。
        """
        btn = ASK_STATUS_BTN.get(status, "")
        params = {"card_status": status}
        if btn:
            params["form_btn_text"] = btn
        if summary:
            params["question_desc"] = summary
        try:
            self._card.update_vars(**params)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("提问卡终态化失败（%s）：%s", status, e)
            return False

