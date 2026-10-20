"""钉钉主动推送 —— 让项目里**任何脚本**都能把消息推到用户钉钉 1:1 会话。

与「被动回复」的区别：listen_up_dingtalk_v3.py 只能在你发消息时回复，
本模块可以**主动**推送（定时任务、交易提醒、报告送达等）。

用法（代码）：
    from tools.dingtalk_push import push_markdown, push_image, push_file, push_files
    push_markdown("**提醒**：中证1000 跌破 6600，触发止损", title="交易提醒")
    push_image("chart.png")
    push_file(r"D:\\report\\2026早报.pdf")
    push_files(["a.xlsx", "b.csv"])          # 一次发多个

用法（命令行）：
    python tools/dingtalk_push.py content.txt "标题"     # 推文字
    python tools/dingtalk_push.py --test                # 发一条测试消息
    python tools/dingtalk_push.py --image chart.png     # 推图片
    python tools/dingtalk_push.py --file a.pdf b.xlsx   # 推一个或多个文件
    echo "内容" | python tools/dingtalk_push.py -       # 从 stdin 读文字

支持的文件类型不限（PDF / Excel / CSV / 日志 / 代码 / 压缩包…），
但受钉钉素材接口限制，单个文件需 ≤20MB。

接收方（robot_code + userId）取自 tools/.dingtalk_recipient.json，
该文件由 finanCalc/listen_up_dingtalk_v3.py 在收到消息时自动写入；
若不存在，先给钉钉机器人发一条消息即可。

为什么不用 session_webhook：那个会过期。这里用钉钉官方 1:1 主动推送通道
`/v1.0/robot/oToMessages/batchSend` + robotCode + userIds，长期有效。
"""
from __future__ import annotations

import json
import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MAINTOY = os.path.dirname(HERE)
for _p in (HERE, MAINTOY):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import requests  # noqa: E402

from notifycation import _get_app_access_token  # noqa: E402

log = logging.getLogger("dingtalk_push")

RECIPIENT_FILE = os.path.join(HERE, ".dingtalk_recipient.json")
ENDPOINT = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
DEFAULT_TITLE = "金融助理分析"
MAX_CHARS = 18000          # 钉钉 markdown 正文有长度上限，超出截断


def load_recipient() -> dict:
    """读取最近活跃用户（robot_code / staff_id）。"""
    try:
        with open(RECIPIENT_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def push_markdown(text: str, title: str = DEFAULT_TITLE,
                  to_staff_id: str | None = None) -> bool:
    """主动推送一条 markdown 消息到钉钉（机器人 1:1 会话）。

    :param text: markdown 正文
    :param title: 消息标题（钉钉列表里显示）
    :param to_staff_id: 指定接收人；默认用 .dingtalk_recipient.json 里的最近活跃用户
    :return: 是否成功
    """
    text = (text or "").strip()
    if not text:
        log.warning("推送内容为空，跳过")
        return False
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n\n…（内容过长已截断）"

    rec = load_recipient()
    robot_code = rec.get("robot_code")
    user_id = to_staff_id or rec.get("sender_staff_id")
    if not (robot_code and user_id):
        log.error("缺少 robot_code / staff_id：请先给钉钉机器人发一条消息"
                  "（机器人收到消息时会写入 %s）", RECIPIENT_FILE)
        return False

    tok = _get_app_access_token()
    if not tok:
        log.error("获取钉钉 access_token 失败（检查 .env 里的 APP_KEY / APP_SECRET）")
        return False

    payload = {
        "robotCode": robot_code,
        "userIds": [user_id],
        "msgKey": "sampleMarkdown",
        "msgParam": json.dumps({"title": title, "text": text}, ensure_ascii=False),
    }
    try:
        r = requests.post(
            ENDPOINT, json=payload, timeout=15,
            headers={"x-acs-dingtalk-access-token": tok,
                     "Content-Type": "application/json"},
        )
    except Exception as exc:  # noqa: BLE001
        log.error("推送异常: %s", exc)
        return False

    body = {}
    if r.status_code == 200:
        try:
            body = r.json()
        except ValueError:
            body = {}
    bad = body.get("invalidStaffIdList") or body.get("filteredStaffIdList")
    if r.status_code in (200, 204) and not bad:
        log.info("推送成功 -> %s（%d 字，title=%s）", user_id, len(text), title)
        return True
    log.error("推送失败: HTTP %s %s", r.status_code, r.text[:300])
    return False


def _resolve_target(to_staff_id: str | None) -> tuple:
    """返回 (robot_code, user_id)；任一缺失则记日志并返回 (None, None)。"""
    rec = load_recipient()
    robot_code = rec.get("robot_code")
    user_id = to_staff_id or rec.get("sender_staff_id")
    if not (robot_code and user_id):
        log.error("缺少 robot_code / staff_id：请先给钉钉机器人发一条消息")
        return None, None
    return robot_code, user_id


def _work_notice(user_id: str, msgtype: str, payload: dict) -> bool:
    """工作通知通道（asyncsend_v2）：图片/文件都能**内联**显示在钉钉里。

    这是本地文件唯一能做到"直接可见"的通道（机器人消息的 image 类型
    需要公网 URL，本地用不了）。
    """
    from notifycation import _load_env_key

    agent_id = _load_env_key("AGENT_ID") or "4489098855"
    tok = _get_app_access_token()
    if not tok:
        return False
    try:
        r = requests.post(
            "https://oapi.dingtalk.com/topapi/message/corpconversation/asyncsend_v2",
            params={"access_token": tok},
            json={"agent_id": int(agent_id), "userid_list": user_id,
                  "msg": {"msgtype": msgtype, msgtype: payload}},
            timeout=15,
        )
        if r.status_code == 200 and r.json().get("errcode") == 0:
            return True
        log.warning("工作通知(%s)失败: %s", msgtype, r.text[:200])
    except Exception as exc:  # noqa: BLE001
        log.warning("工作通知(%s)异常: %s", msgtype, exc)
    return False


def _send_robot_file(robot_code: str, user_id: str, media_id: str,
                     name: str, ext: str) -> bool:
    """机器人单聊文件消息（点开可下载/查看）——图片与文件的通用兜底通道。"""
    tok = _get_app_access_token()
    if not tok:
        return False
    try:
        r = requests.post(
            ENDPOINT, timeout=20,
            headers={"x-acs-dingtalk-access-token": tok,
                     "Content-Type": "application/json"},
            json={"robotCode": robot_code, "userIds": [user_id],
                  "msgKey": "sampleFile",
                  "msgParam": json.dumps({"mediaId": media_id, "fileName": name,
                                          "fileType": ext}, ensure_ascii=False)},
        )
    except Exception as exc:  # noqa: BLE001
        log.error("文件消息异常: %s", exc)
        return False
    return r.status_code in (200, 204)


def push_file(file_path: str, to_staff_id: str | None = None) -> bool:
    """主动推送**任意本地文件**（PDF / Excel / 日志 / 代码 / 报告…）到钉钉。

    通道优先级：
      1) 工作通知 asyncsend_v2 + msgtype=file → 钉钉里直接看到文件卡片（首选）
      2) 机器人单聊 sampleFile → 点开查看（兜底）

    注意：钉钉对素材大小有限制（图片/文件一般 ≤20MB），超限会在这里明确报错。
    """
    from notifycation import _upload_media_get_media_id

    if not os.path.isfile(file_path):
        log.error("文件不存在: %s", file_path)
        return False
    size_mb = os.path.getsize(file_path) / 1024 / 1024
    if size_mb > 20:
        log.error("文件过大（%.1fMB > 20MB），钉钉素材接口会拒绝: %s",
                  size_mb, file_path)
        return False

    robot_code, user_id = _resolve_target(to_staff_id)
    if not user_id:
        return False

    name = os.path.basename(file_path)
    ext = os.path.splitext(name)[1].lstrip(".") or "bin"

    media_id = _upload_media_get_media_id(file_path, media_type="file")
    if media_id and _work_notice(user_id, "file", {"media_id": media_id}):
        log.info("文件推送成功（工作通知）-> %s: %s（%.2fMB）",
                 user_id, name, size_mb)
        return True
    if not media_id:
        media_id = _upload_media_get_media_id(file_path, media_type="file")
    if not media_id:
        log.error("文件上传失败（检查 APP_KEY / APP_SECRET / AGENT_ID）")
        return False
    if _send_robot_file(robot_code, user_id, media_id, name, ext):
        log.info("文件推送成功（机器人文件消息）-> %s: %s", user_id, name)
        return True
    return False


def push_files(paths, to_staff_id: str | None = None) -> tuple:
    """一次推送多个文件。返回 (成功数, 失败数)。"""
    if isinstance(paths, (str, bytes)):
        paths = [paths]
    ok = bad = 0
    for p in paths:
        if push_file(str(p), to_staff_id):
            ok += 1
        else:
            bad += 1
    return ok, bad


def push_image(image_path: str, title: str = DEFAULT_TITLE,
               to_staff_id: str | None = None) -> bool:
    """主动推送一张**本地图片**到钉钉。

    钉钉机器人 1:1 的 sampleImageMsg 需要**公网 URL**，本地文件用不了，
    所以走两条官方通道（无需公网）：
      1) 工作通知 asyncsend_v2 + msgtype=image + media_id → 图片**内联**显示（首选）
      2) 机器人单聊文件消息 sampleFile + mediaId → 需点开查看（兜底）

    限制：同一应用同一用户每天 ≤500 条；**内容完全相同的图片每天只能发一次**。
    所以每次推送建议生成内容不同的图（例如带上生成时间）。
    """
    from notifycation import _upload_media_get_media_id

    if not os.path.isfile(image_path):
        log.error("图片不存在: %s", image_path)
        return False

    robot_code, user_id = _resolve_target(to_staff_id)
    if not user_id:
        return False

    # 1) 工作通知（图片内联，体验最好）
    media_id = _upload_media_get_media_id(image_path, media_type="image")
    if media_id and _work_notice(user_id, "image", {"media_id": media_id}):
        log.info("图片推送成功（工作通知·内联）-> %s: %s", user_id, image_path)
        return True

    # 2) 兜底：机器人单聊文件消息
    media_id = media_id or _upload_media_get_media_id(image_path, media_type="file")
    if not media_id:
        log.error("图片上传失败（检查 APP_KEY / APP_SECRET / AGENT_ID）")
        return False
    name = os.path.basename(image_path)
    ext = os.path.splitext(name)[1].lstrip(".") or "png"
    if _send_robot_file(robot_code, user_id, media_id, name, ext):
        log.info("图片推送成功（机器人文件消息）-> %s", user_id)
        return True
    log.error("图片推送失败: HTTP %s %s", r.status_code, r.text[:200])
    return False


def push_text(text: str, to_staff_id: str | None = None) -> bool:
    """推送纯文本消息（不解析 markdown）。"""
    rec = load_recipient()
    robot_code = rec.get("robot_code")
    user_id = to_staff_id or rec.get("sender_staff_id")
    if not (robot_code and user_id):
        log.error("缺少 robot_code / staff_id，无法推送")
        return False
    tok = _get_app_access_token()
    if not tok:
        return False
    payload = {
        "robotCode": robot_code,
        "userIds": [user_id],
        "msgKey": "sampleText",
        "msgParam": json.dumps({"content": (text or "").strip()},
                               ensure_ascii=False),
    }
    try:
        r = requests.post(ENDPOINT, json=payload, timeout=15,
                          headers={"x-acs-dingtalk-access-token": tok,
                                   "Content-Type": "application/json"})
    except Exception as exc:  # noqa: BLE001
        log.error("推送异常: %s", exc)
        return False
    return r.status_code in (200, 204)


def _main(argv: list) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--test":
        rec = load_recipient()
        ok = push_markdown(
            "**测试消息**\n\n"
            f"- 目标：{rec.get('sender_nick', '?')}（{rec.get('sender_staff_id', '?')}）\n"
            f"- 机器人：{rec.get('robot_code', '?')}\n\n"
            "如果你看到这条消息，说明主动推送通道正常。",
            title="推送测试")
        return 0 if ok else 1

    if argv[0] in ("--image", "-i"):
        if len(argv) < 2:
            print("用法: python tools/dingtalk_push.py --image <图片路径>")
            return 1
        return 0 if push_image(argv[1]) else 1

    if argv[0] in ("--file", "-f"):
        if len(argv) < 2:
            print("用法: python tools/dingtalk_push.py --file <文件1> [文件2] ...")
            return 1
        ok, bad = push_files(argv[1:])
        print(f"文件推送：成功 {ok} 个，失败 {bad} 个")
        return 0 if bad == 0 else 1

    src = argv[0]
    title = argv[1] if len(argv) > 1 else DEFAULT_TITLE
    if src == "-":
        text = sys.stdin.read()
    else:
        with open(src, "r", encoding="utf-8") as fh:
            text = fh.read()
    return 0 if push_markdown(text, title=title) else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
