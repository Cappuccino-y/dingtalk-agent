import os
import json
import logging
import requests
from urllib.parse import urlencode
import urllib.parse
import time
import hmac
import hashlib
import base64

logger = logging.getLogger(__name__)

WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=9323cbca8c62c807654a4d134d3ba36e5deb8b0de75fa418b4b0fed3a8bed6c8"
SECRET = "SEC482c404e44ae1f4d5ed3deb10786d4fdbbea74a9e9f0f533e2b5093bc0fff66b"
ROBOT_ACCESS_TOKEN = WEBHOOK.split("access_token=", 1)[1]
MEDIA_UPLOAD_URL = "https://oapi.dingtalk.com/media/upload"
DINGTALK_OPENAPI_ENDPOINT = "https://api.dingtalk.com"

# 应用级 access_token 缓存（上传图片素材需要企业应用的 token，而非机器人 token）
_APP_ACCESS_TOKEN: str | None = None
_APP_ACCESS_TOKEN_EXPIRE_AT: float = 0.0


RECIPIENT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".dingtalk_recipient.json")


def _load_recipient() -> dict:
    """读取 listen_up_dingtalk_v2.py 持久化的最近活跃用户会话信息"""
    try:
        with open(RECIPIENT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _signed_webhook_url() -> str:
    """构造带时间戳+签名的 webhook URL（钉钉机器人加签方式）"""
    timestamp = str(round(time.time() * 1000))
    sign_str = f"{timestamp}\n{SECRET}"
    hmac_code = hmac.new(SECRET.encode(), sign_str.encode(), digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return f"{WEBHOOK}&timestamp={timestamp}&sign={sign}"


def send_dingtalk_message(content):
    """发送钉钉机器人Markdown消息"""
    url = _signed_webhook_url()

    headers = {"Content-Type": "application/json"}
    data = {
        "msgtype": "markdown",
        "markdown": {
            "title": "金融技术提醒",
            "text": content
        },
        "at": {
            "isAtAll": True
        }
    }

    try:
        response = requests.post(url, json=data, headers=headers, timeout=10)
        if response.status_code == 200:
            logger.info("钉钉消息发送成功")
            return True
        else:
            logger.error(f"发送失败：{response.text}")
    except Exception as e:
        logger.error(f"网络异常：{str(e)}")
    return False


def _load_env_key(name: str):
    """从环境变量读取，失败时回退到 MainToy/.env（支持 `KEY="value"` 格式）"""
    val = os.getenv(name)
    if val:
        return val.strip().strip('"').strip("'")
    env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    try:
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _get_app_access_token():
    """用 APP_KEY/APP_SECRET 获取企业应用 access_token（media/upload 需要它）"""
    global _APP_ACCESS_TOKEN, _APP_ACCESS_TOKEN_EXPIRE_AT
    now = time.time()
    if _APP_ACCESS_TOKEN and now < _APP_ACCESS_TOKEN_EXPIRE_AT:
        return _APP_ACCESS_TOKEN
    app_key = _load_env_key("APP_KEY")
    app_secret = _load_env_key("APP_SECRET")
    if not app_key or not app_secret:
        logger.error("未配置 APP_KEY/APP_SECRET（环境变量或 MainToy/.env），无法上传图片")
        return None
    try:
        r = requests.post(
            f"{DINGTALK_OPENAPI_ENDPOINT}/v1.0/oauth2/accessToken",
            json={"appKey": app_key, "appSecret": app_secret},
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        _APP_ACCESS_TOKEN = data["accessToken"]
        _APP_ACCESS_TOKEN_EXPIRE_AT = now + data["expireIn"] - 300
        return _APP_ACCESS_TOKEN
    except Exception as e:
        logger.error(f"获取钉钉应用 access_token 失败: {e}")
        return None


def _upload_media_get_media_id(file_path: str, media_type: str = "file"):
    """上传本地文件到钉钉素材库，返回 media_id；失败返回 None

    media_type: image / file / voice / video（sampleFile 需用 file 类型上传）
    """
    access_token = _get_app_access_token()
    if not access_token:
        return None
    try:
        with open(file_path, "rb") as f:
            files = {"media": (os.path.basename(file_path), f, "application/octet-stream")}
            values = {"type": media_type}
            r = requests.post(
                f"{MEDIA_UPLOAD_URL}?access_token={access_token}",
                data=values,
                files=files,
                timeout=30,
            )
        if r.status_code == 200:
            data = r.json()
            if "media_id" in data:
                return data["media_id"]
            logger.error(f"钉钉文件上传失败: {data}")
        else:
            logger.error(f"钉钉文件上传 HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.error(f"钉钉文件上传异常: {e}")
    return None


def _send_work_notice_image(user_id: str, media_id: str) -> bool:
    """工作通知（asyncsend_v2）：以应用名义发完整图片消息，显示在钉钉「工作通知」。

    官方教程路径（上传媒体 → 发送工作通知），图片完整内联显示，不需要公网 URL。
    限制：同一应用同一用户每天最多 500 条；相同内容每天只能一次。
    """
    agent_id = _load_env_key("AGENT_ID") or "4489098855"
    tok = _get_app_access_token()
    if not tok:
        return False
    try:
        r = requests.post(
            "https://oapi.dingtalk.com/topapi/message/corpconversation/asyncsend_v2",
            params={"access_token": tok},
            json={
                "agent_id": int(agent_id),
                "userid_list": user_id,
                "msg": {"msgtype": "image", "image": {"media_id": media_id}},
            },
            timeout=10,
        )
        data = r.json()
        if data.get("errcode") == 0:
            logger.info(f"工作通知图片发送成功 (task_id={data.get('task_id')})")
            return True
        logger.error(f"工作通知图片发送失败: {data}")
    except Exception as e:
        logger.error(f"工作通知图片发送异常: {e}")
    return False


def send_dingtalk_image(image_path: str, title: str = "图片通知") -> bool:
    """把图片（如微信登录二维码）发给最近活跃用户。

    通道优先级（钉钉机器人「图片」消息要求公网 URL，本地文件无法直接内联发图）：
    1. 工作通知 asyncsend_v2（msgtype=image + media_id，完整图片，显示在「工作通知」）
    2. 企业机器人单聊文件消息（sampleFile + media_id，点开可查看/扫码）
    3. 兜底：群 webhook 文字告警（提示给机器人发「重新授权」）
    """
    if not os.path.exists(image_path):
        logger.error(f"图片文件不存在: {image_path}")
        return False

    recipient = _load_recipient()
    robot_code = recipient.get("robot_code")
    user_id = recipient.get("sender_staff_id")
    if not (robot_code and user_id):
        send_dingtalk_message(
            f"### ⚠️ {title}\n\n"
            "没有可用的机器人会话（请先给钉钉机器人发一条消息，例如「重新授权」）。"
        )
        return False

    # 1) 工作通知完整图片
    media_id = _upload_media_get_media_id(image_path, media_type="image")
    if media_id and _send_work_notice_image(user_id, media_id):
        return True

    # 2) 机器人单聊文件消息（点开可扫码）
    media_id = _upload_media_get_media_id(image_path, media_type="file")
    if not media_id:
        send_dingtalk_message(
            f"### ⚠️ {title}\n\n无法自动推送二维码（上传失败），"
            "请给钉钉机器人发送「重新授权」获取二维码。"
        )
        return False

    tok = _get_app_access_token()
    if not tok:
        return False

    name = os.path.basename(image_path)
    ext = os.path.splitext(name)[1].lstrip(".") or "png"
    # 文件名带清晰后缀，方便手机上识别/预览（钉钉文件消息 fileName 需含扩展名）
    if title and "微信" in title:
        display_name = f"微信登录二维码.{ext}"
    else:
        display_name = name if name.lower().endswith(f".{ext}") else f"{name}.{ext}"
    try:
        r = requests.post(
            f"{DINGTALK_OPENAPI_ENDPOINT}/v1.0/robot/oToMessages/batchSend",
            json={
                "robotCode": robot_code,
                "userIds": [user_id],
                "msgKey": "sampleFile",
                "msgParam": json.dumps({
                    "mediaId": media_id,
                    "fileName": display_name,
                    "fileType": ext,
                }),
            },
            headers={"x-acs-dingtalk-access-token": tok,
                     "Content-Type": "application/json"},
            timeout=10,
        )
        body = r.json() if r.status_code == 200 else {}
        bad = body.get("invalidStaffIdList") or body.get("filteredStaffIdList")
        if r.status_code in (200, 204) and not bad:
            logger.info(f"钉钉文件消息发送成功: {title}")
            return True
        logger.error(f"钉钉文件消息发送失败: HTTP {r.status_code} {r.text[:200]}")
    except Exception as e:
        logger.error(f"钉钉文件消息发送异常: {e}")

    send_dingtalk_message(
        f"### ⚠️ {title}\n\n"
        "无法自动推送二维码（消息通道异常），请给钉钉机器人发送「重新授权」获取二维码。"
    )
    return False


if __name__ == "__main__":
    send_dingtalk_message("""
==================================================
📈 早间财经快讯汇总报告
==================================================
好的，作为一名专注于A股市场，特别是中证1000指数的宏观策略分析师，我将严格依据您提供的隔夜财经快讯和多周期技术面数据，遵循既定分析框架，进行如下专业研判。

---

### **1. 核心消息摘要**

1.  **高层释放对外开放信号，桥水达利欧看好中国经济前景。**
    *   **影响：利多（情绪面）。** 高层会见国际知名投资者，释放积极信号，有助于提振外资信心和市场情绪。
2.  **美国2月核心PCE物价指数符合预期，但消费者支出几无增长。**
    *   **影响：中性偏多（对A股）。** 数据未超预期，缓解了市场对通胀再度飙升的担忧。同时，消费疲软可能减缓美联储紧缩步伐，对全球流动性环境构成边际利好。
3.  **地缘局势紧张与谈判预期交织，油价大幅波动。**
    *   **影响：复杂（偏空）。** 伊朗、黎巴嫩、以色列局势依然紧张，油价盘中暴涨超8%，加剧全球通胀和增长不确定性。但后续消息显示美国施压以色列、谈判将继续，部分缓解了紧张情绪（油价回落）。整体对A股风险偏好构成压制。
4.  **四部门召开动力电池行业座谈会，强调治理“内卷外化”。**
    *   **影响：中性偏多（结构性）。** 对部分过度竞争的行业（如锂电池）是政策层面的规范信号，短期可能压制相关板块情绪，但中长期有利于行业龙头和健康发展。
5.  **财政部、交通部联合发文，支持新一轮国家综合货运枢纽补链强链。**
    *   **影响：利多（结构性）。** 明确的产业政策支持，利好物流、交通基础设施及先进制造相关产业链，对中证1000指数中的相关中小市值公司构成主题性驱动。

### **2. 情绪综合研判**

*   **当前市场情绪偏向：中性偏谨慎。**
*   **主要驱动因素：**
    1.  **内外情绪分化：** 国内政策面暖风频吹（对外开放、产业政策），但隔夜海外地缘风险（中东局势、油价飙升）对全球风险偏好构成显著压制，纳斯达克中国金龙指数收跌也反映了这种压力。
    2.  **技术面显示分歧：** 前一日（4月9日）中证1000指数高开低走，最终仅微涨0.48%，收长上影线，显示在7950点上方遭遇明显抛压，部分国内利好可能已被盘中消化。尾盘虽小幅拉升，但量能并未显著放大，表明追高意愿不强。
    3.  **定价情况：** 国内政策利好（如高层会见、行业政策）在昨日盘中已有反应，但未能推动指数有效突破。隔夜油价的剧烈波动和地缘政治的不确定性，是今日市场需要消化的新变量，可能加剧观望情绪。

### **3. 多周期技术面点评**

*   **短期趋势（5分钟/15分钟）：** 陷入窄幅震荡。均线系统高度缠绕（MA5, MA10, MA20），方向不明。尾盘（15:00）5分钟和15分钟K线均放量拉升，但未能突破日内的主要震荡区间（约7910-7950）。**关键短期压力在7950-7960一带**（昨日高点及前期密集成交区），**支撑在7900-7910一带**（昨日尾盘拉升起点及均线密集区）。
*   **日线趋势：** 处于超跌反弹后的震荡整理阶段。指数站上所有短期均线（MA5, MA10, MA20），显示短期趋势转强。但**中期均线MA60（约8127）仍在上方构成强压**，且目前位置（7921）离其尚有距离。昨日收带上影线的阳线，量能正常，表明多空在当前位置分歧加大。**日线级别关键支撑上移至MA20（约7828）附近。**
*   **周线趋势：** 仍处于下行趋势中。指数运行在周线MA5和MA10下方，表明中期调整格局未变。近期周K线显示在7500点附近有较强支撑，但反弹力度受制于上方均线压力。

### **4. 今日走势预判**

*   **基准情景（概率较高）：低开或平开，区间震荡，消化内外消息。**
    *   **理由：** 隔夜油价剧烈波动及地缘不确定性将导致市场开盘偏谨慎，可能低开。但国内政策暖意和指数短期均线支撑会封杀大幅下跌空间。市场将进入观望模式，等待更明确信号。
    *   **运行区间：预计在7880-7950点之间震荡。** 上方关注7950点压力，下方关注7900点及日线MA5（约7734，距离较远）的支撑。
*   **风险情景（需警惕）：低开低走，考验下方支撑。**
    *   **触发条件：** 若中东局势在亚洲交易时段出现恶化消息，或油价再度大幅上涨，可能引发全球避险情绪升温，外资流出压力加大。
    *   **走势描述：** 指数可能低开后震荡走低，有效跌破7900点支撑，并向日线MA20（7828点）附近寻求支撑。成交量可能放大，显示抛压加重。

### **5. 风险提示**

1.  **地缘政治风险：** 中东局势（伊朗、以色列、黎巴嫩）是最大不确定性。任何谈判破裂或冲突升级的消息，都将直接冲击全球市场风险偏好，并通过油价传导至通胀和增长预期。
2.  **海外市场波动：** 需密切关注美股尤其是科技股的后续走势，以及美元、美债收益率的变动，这些都将影响外资流向和A股估值。
3.  **国内政策跟进：** 虽然政策暖风频吹，但市场需要看到更实质性的经济数据改善或增量资金入市信号，才能形成持续的向上动力。
4.  **技术面关键位得失：** 今日能否守住7900点并重新站上7950点，对短期市场情绪至关重要。若失守关键支撑，可能引发技术性卖盘。

---
**免责声明：** 以上分析完全基于您提供的有限信息和历史数据，仅为市场情景推演，不构成任何投资建议。市场有风险，投资需谨慎。
==================================================
    """)