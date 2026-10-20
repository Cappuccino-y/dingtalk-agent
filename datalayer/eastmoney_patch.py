"""
东方财富反爬补丁 (ENABLE_EASTMONEY_PATCH)
来源: https://github.com/ZhuLinsen/daily_stock_analysis/blob/main/src/patches/eastmoney_patch.py
"""
import hashlib
import random
import secrets
import threading
import time
import requests
import json
import uuid
import logging
from fake_useragent import UserAgent

logger = logging.getLogger(__name__)

original_request = requests.Session.request

try:
    ua = UserAgent()
except Exception:
    # 如果 fake-useragent 初始化失败，使用备用 UA 列表
    class FallbackUA:
        def __init__(self):
            self._uas = [
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            ]
            self._idx = 0
        @property
        def random(self):
            ua = random.choice(self._uas)
            return ua
    ua = FallbackUA()


class AuthCache:
    def __init__(self):
        self.data = None
        self.expire_at = 0
        self.lock = threading.Lock()
        self.ttl = 20


_cache = AuthCache()


class PatchSign:
    def __init__(self):
        self.patched = False

    def set_patch(self, patched):
        self.patched = patched

    def is_patched(self):
        return self.patched


_patch_sign = PatchSign()


def _get_nid(user_agent):
    """获取东方财富的 NID 授权令牌"""
    now = time.time()
    if _cache.data and now < _cache.expire_at:
        return _cache.data

    with _cache.lock:
        try:
            def generate_uuid_md5():
                unique_id = str(uuid.uuid4())
                return hashlib.md5(unique_id.encode('utf-8')).hexdigest()

            def generate_st_nvi():
                HASH_LENGTH = 4
                def generate_random_string(length=21):
                    charset = "useandom-26T198340PX75pxJACKVERYMINDBUSHWOLF_GQZbfghjklqvwyzrict"
                    return ''.join(secrets.choice(charset) for _ in range(length))
                def sha256(input_str):
                    return hashlib.sha256(input_str.encode('utf-8')).hexdigest()
                random_str = generate_random_string()
                hash_prefix = sha256(random_str)[:HASH_LENGTH]
                return random_str + hash_prefix

            url = "https://anonflow2.eastmoney.com/backend/api/webreport"
            screen_resolution = random.choice(['1920X1080', '2560X1440', '3840X2160'])
            payload = json.dumps({
                "osPlatform": "Windows",
                "sourceType": "WEB",
                "osversion": "Windows 10.0",
                "language": "zh-CN",
                "timezone": "Asia/Shanghai",
                "webDeviceInfo": {
                    "screenResolution": screen_resolution,
                    "userAgent": user_agent,
                    "canvasKey": generate_uuid_md5(),
                    "webglKey": generate_uuid_md5(),
                    "fontKey": generate_uuid_md5(),
                    "audioKey": generate_uuid_md5()
                }
            })
            headers = {
                'Cookie': f'st_nvi={generate_st_nvi()}',
                'Content-Type': 'application/json'
            }
            response = requests.request("POST", url, headers=headers, data=payload, timeout=30)
            response.raise_for_status()

            data = response.json()
            nid = data['data']['nid']

            _cache.data = nid
            _cache.expire_at = now + _cache.ttl
            return nid
        except requests.exceptions.RequestException as e:
            logger.warning(f"请求东方财富授权接口失败: {e}")
            _cache.data = None
            _cache.expire_at = now + 5 * 60
            return None
        except (KeyError, json.JSONDecodeError) as e:
            logger.warning(f"解析东方财富授权接口响应失败: {e}")
            _cache.data = None
            _cache.expire_at = now + 5 * 60
            return None


def enable_eastmoney_patch():
    """启用东方财富反爬补丁"""
    if _patch_sign.is_patched():
        return

    def patched_request(self, method, url, **kwargs):
        is_target = any(
            d in (url or "")
            for d in [
                "fund.eastmoney.com",
                "push2.eastmoney.com",
                "push2his.eastmoney.com",
            ]
        )
        if not is_target:
            return original_request(self, method, url, **kwargs)

        user_agent = ua.random
        headers = kwargs.get("headers", {})
        headers["User-Agent"] = user_agent
        nid = _get_nid(user_agent)
        if nid:
            headers["Cookie"] = f"nid18={nid}"
        kwargs["headers"] = headers

        sleep_time = random.uniform(1, 3)
        time.sleep(sleep_time)
        return original_request(self, method, url, **kwargs)

    requests.Session.request = patched_request
    _patch_sign.set_patch(True)
    print("[EastMoney Patch] 已启用 - NID 令牌 + 随机 UA + 请求延迟")
