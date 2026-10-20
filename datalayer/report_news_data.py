import requests
import re
import json
import time
import random
import logging
from datetime import datetime, timedelta, time as dt_time
import sys
import pandas as pd
sys.path.append(r'D:\PythonProject\MainToy')
from tools import llm_client,notifycation
from baostock_daily import IndexDataFetcher
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

import os
import json

HISTORY_DIR = "./conversation_history"  # 存放历史文件的目录

def get_history_filepath(date_str: str = None) -> str:
    """返回指定日期的历史文件路径"""
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
    os.makedirs(HISTORY_DIR, exist_ok=True)
    return os.path.join(HISTORY_DIR, f"conversation_{date_str}.json")

def load_shared_history(date_str: str = None) -> list:
    """加载共享对话历史（用于定时任务和手动接口读取）"""
    filepath = get_history_filepath(date_str)
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def save_shared_history(history: list, date_str: str = None):
    """保存共享对话历史（仅定时任务调用）"""
    filepath = get_history_filepath(date_str)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

import copy
# ---------- 带对话历史的 AI 调用封装 ----------
def call_ai_with_history(ai_client, user_prompt: str, system_prompt: str,
                         history: list, copy_history: bool = False) -> tuple:
    """
    调用 AI 的 chat 接口，自动维护对话历史。
    新增参数 copy_history: 若为 True，则复制历史后再操作，避免影响原列表。
    """
    if copy_history:
        history = copy.deepcopy(history)

    # 原有逻辑不变
    if not history:
        history = [{"role": "system", "content": system_prompt}]
    history.append({"role": "user", "content": user_prompt})
    response = ai_client.chat(messages=history)
    history.append({"role": "assistant", "content": response})
    return response, history

def is_trading_day(date: datetime = None) -> bool:
    if date is None:
        date = datetime.now()
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        # 将 trade_date 列转为 datetime 类型
        df['trade_date'] = pd.to_datetime(df['trade_date'])
        target_date = pd.to_datetime(date.date())
        return (df['trade_date'] == target_date).any()
    except Exception as e:
        logging.warning(f"获取交易日历失败: {e}，使用简易判断（周一至周五）")
        return date.weekday() < 5

def build_morning_prompt(news_list: list, tech_data_text: str) -> tuple:
    """构建早间（开盘前）双周期交易策略 Prompt"""
    system_prompt = """你是一位专注于中证1000指数的量化交易策略师。当前为**早间开盘前**，你将收到上一日收盘后的重要财经快讯及完整的多周期技术面数据。你的任务是基于这些信息，制定一份**具有可执行性**的双周期交易参考报告，涵盖**短线交易**与**波段交易**两个维度。

请严格遵循以下输出框架，确保所有建议均引用提供的技术数据作为逻辑支撑，**禁止臆测具体点位**：

**报告框架：**
1.  **核心消息与情绪研判**
    - 提炼最重要的3条消息，标注多空影响。
    - 综合判断今日早盘市场主导情绪（乐观/悲观/中性）及潜在波动率预期。

2.  **技术面关键位梳理**
    - 从数据中提取并列出今日最重要的**支撑位**和**压力位**（基于均线、近期高低点、缺口位置）。

3.  **短线交易策略（持仓周期：日内至1-2天）**
    - **交易方向**：做多 / 做空 / 观望。
    - **入场参考区**：结合开盘情绪及技术支撑/压力，给出一个合理的**价格区间**。
    - **止盈参考区**：基于上方压力位或下方支撑位设定。
    - **止损参考位**：基于关键支撑/压力位外侧设定，必须提供明确的风控点位。
    - **逻辑依据**：简要说明基于哪条均线、量价关系或分时结构得出此策略。

4.  **波段交易策略（持仓周期：3-10个交易日）**
    - **交易方向**：做多 / 做空 / 观望。
    - **入场参考区**：基于日线或周线级别的趋势结构给出。
    - **止盈参考区**：基于中期压力/支撑位设定。
    - **止损参考位**：基于中期关键颈线或均线外侧设定。
    - **逻辑依据**：重点引用日线均线排列、周线趋势状态及大级别量价配合情况。

5.  **风险警示**
    - 指出可能导致策略失效的突发变量（如午间突发利空、北向资金大幅异动等）。

**注意事项：**
- 严格基于提供的数据，若数据不足以支撑某一方向的策略，请明确输出 **“当前信号矛盾，建议观望”** 。
- 保持客观，使用“参考区”、“附近”、“一带”等非绝对化表述。
"""

    news_text = "\n".join([
        f"- {item.get('time', '未知时间')}: {item.get('data', {}).get('content', '无内容')}"
        for item in news_list
    ]) if news_list else "（无重要快讯）"

    user_prompt = f"""请根据以下信息，为今日中证1000指数制定开盘后的双周期交易参考策略。

========================================
一、重要财经快讯（上一日收盘后至今）
========================================
{news_text}

========================================
二、日韩指数早盘数据及中证1000多周期技术面数据
========================================
{tech_data_text}
"""
    return system_prompt, user_prompt


def build_noon_prompt(news_list: list, tech_data_text: str) -> tuple:
    """构建午间（盘中修正）双周期交易策略 Prompt"""
    system_prompt = """你是一位专注于中证1000指数的量化交易策略师。当前为**午间休市时段**，你将收到上午盘中重要快讯、最新的多周期技术面数据，以及上午的实际走势信息。你的任务是修正并更新**短线**与**波段**两个维度的交易参考策略。

请严格遵循以下输出框架：

**报告框架：**
1.  **上午走势复盘与关键变化**
    - 简述上午走势，重点指出**技术面关键位**（支撑/压力）是否被触及或突破。
    - 对比早间情绪，判断市场实际强弱。

2.  **午间新增消息点评**
    - 提炼上午盘中最重要的消息，评估其对午后情绪的影响。

3.  **短线交易策略（午后修正）**
    - **交易方向**：做多 / 做空 / 观望。
    - **入场参考区**：结合上午形成的分时高低点及均线位置重新评估。
    - **止盈参考区**：午后潜在的波动目标位。
    - **止损参考位**：午后破位即走的关键防守位。
    - **逻辑依据**：重点说明上午的量价配合情况（如放量滞涨、缩量企稳等）。

4.  **波段交易策略（中期视角修正）**
    - **交易方向**：做多 / 做空 / 观望。
    - **入场参考区**：若上午走势对日线结构产生影响，更新入场区域；否则维持早间观点。
    - **止盈参考区** / **止损参考位**：根据最新日线/周线数据更新。
    - **逻辑依据**。

5.  **午后风险提示**
    - 关注尾盘异动、北向资金流向等不确定因素。

**注意事项：**
- 若上午走势已验证早间策略（例如已触发止盈止损），应基于新数据提供下一阶段策略，而非固守原观点。
"""

    news_text = "\n".join([
        f"- {item.get('time', '未知时间')}: {item.get('data', {}).get('content', '无内容')}"
        for item in news_list
    ]) if news_list else "（上午无重要新增快讯）"

    user_prompt = f"""请根据以下信息，对中证1000指数午后走势进行分析并更新双周期交易策略。

========================================
一、上午盘中重要快讯
========================================
{news_text}

========================================
二、日韩指数午盘数据及中证1000最新多周期技术数据（含上午收盘数据）
========================================
{tech_data_text}
"""
    return system_prompt, user_prompt

def build_manual_report_prompt(scene: str, news_list: list, tech_data_text: str) -> tuple:
    """
    为手动报告构建生成市场分析报告的提示词。
    scene: 开盘前 / 午盘前 / 收盘前 / 收盘后 / 非交易日
    """
    # 场景文本映射
    scene_text = {
        "开盘前": "开盘前",
        "午盘前": "午间休市",
        "收盘前": "尾盘交易时段",
        "收盘后": "收盘后",
        "非交易日": "非交易日休市期间"
    }.get(scene, "当前时段")

    system_prompt = f"""你是一位专业的量化交易顾问，专注于中证1000指数。当前为**{scene_text}**。

请根据最新的市场快讯和技术数据，生成一份结构清晰、重点突出的市场报告。报告必须立足于**中证1000指数**的走势研判，结合消息面与技术面，为投资者提供有价值的参考。

**报告要求：**
- 首先用2-3句话概括当前市场的核心驱动逻辑（消息面+技术面共振或背离）。
- 然后分点阐述关键的技术位（支撑/压力）、形态结构、量价关系，并说明其市场含义。
- 结合近期快讯，分析对指数可能产生的影响，区分短期情绪与中期逻辑。
- 最后给出风险提示以及下一阶段需要重点关注的变量。
- 数据引用要具体，避免模糊表述；历史分析若存在，可引用今日早/午盘报告的验证情况。

**输出格式：**
1. **市场背景与核心逻辑**
2. **技术面关键点位与形态分析**
3. **消息面影响评估**
4. **综合展望与风险提示**
"""

    # 构建新闻摘要
    if news_list:
        news_summary = "\n".join([
            f"- {item.get('time', '未知时间')}: {item.get('data', {}).get('content', '')[:80]}"
            for item in news_list[:5]
        ])
    else:
        news_summary = "（无重要新增快讯）"

    user_prompt = f"""请基于以下信息生成一份中证1000指数的专业市场报告。

【近期重要快讯】
{news_summary}

【最新技术面关键数据】
{tech_data_text}
"""
    return system_prompt, user_prompt

def build_manual_prompt(scene: str, news_list: list, tech_data_text: str, position_prompt: str) -> tuple:
    """
    构建针对用户咨询的积极型提示词。
    scene: 场景标识（可忽略，保留兼容）
    position_prompt: 用户的持仓描述或具体咨询问题
    """
    system_prompt = """你是一位积极进取的量化交易顾问，专注于中证1000指数期货。你的默认立场是**市场总有结构可依托，机会永远存在**。
过度保守而屡荐空仓等同于失职。

**分析原则：**
- 用户会描述持仓状态或提出具体问题，你必须给出完整的交易预案，不能只说"观望"。
- 即使多空交织，也要明确指出哪一方略占优势，并基于此给出一套带止损的试错方案。
- 所有价格均按中证1000指数点位给出，系统会自动换算为IM期货价格，你无需处理基差。
- 使用具体技术位、消息面依据，给出明确的入场区域、止损、止盈。

**根据用户问题类型，按对应格式输出：**

**类型A：实时点位咨询**（如"现在做多/做空点位是多少"）
1. **市场速评**：2-3句概括当前核心驱动力。
2. **方向判断**：做多 / 做空
3. **实时执行参考（指数点位）**：
   - 入场区域
   - 止损位
   - 第一目标区
   - 第二目标区
   - 盈亏比预估
4. **风险提示**：最重要的1条风险。

**类型B：未来计划咨询**（如"明天/周一/节后计划做空/做多的点位"）
1. **当前走势回顾**：1-2句总结近期走势。
2. **未来方向预判**：做多 / 做空 / 震荡
3. **未来计划参考（指数点位）**：
   - 计划入场区域（或条件单触发价）
   - 止损位
   - 第一目标区
   - 第二目标区
   - 盈亏比预估
4. **关键变量**：该时段需要重点关注的1-2个变量。

**类型C：持仓管理咨询**（如"我多单/空单xxx，止盈止损多少"）
1. **持仓诊断**：评估当前持仓的成本价位置（优势/劣势/中性）。
2. **方向判断**：继续持有 / 减仓 / 加仓 / 止损离场
3. **持仓管理参考（指数点位）**：
   - 建议止损位（保护本金）
   - 建议止盈位（第一目标）
   - 建议止盈位（第二目标）
   - 移动止损位（如有浮盈）
4. **风险提示**：最重要的1条风险。

**判断逻辑：**
- 用户提到"现在"+"做多/做空"+"点位" → 类型A
- 用户提到"明天/明日"+"计划"+"做多/做空" → 类型B
- 用户提到"我"+"多单/空单"+"xxx"+"止盈/止损" → 类型C
- 其他问题 → 按最接近的类型处理

请直接按对应格式回答，不要添加额外客套话。
"""

    # 构建新闻摘要（精简为最新3条）
    if news_list:
        news_summary = "\n".join([
            f"- {item.get('time', '未知时间')}: {item.get('data', {}).get('content', '')[:80]}"
            for item in news_list[:3]
        ])
    else:
        news_summary = "（无最新重要快讯）"

    user_prompt = f"""我的问题/持仓：{position_prompt}

请基于以下数据，给我具体的操作建议（含指数点位）。

【最新重要快讯】
{news_summary}

【技术面数据】
{tech_data_text}
"""

    return system_prompt, user_prompt

# ---------- 动态获取最新的 z3c 域名前缀 ----------
# 共享缓存：两个脚本（jin10_news_fetcher.py / report_news_data.py）共用一份
_JIN10_Z3C_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".jin10_z3c_cache.json")
_JIN10_FALLBACK_BASE_URL = "https://3318fc142ea545eab931e22a61ec6e5c.z3c.jin10.com"


def _load_cached_z3c_url() -> str | None:
    """读取本地缓存的 z3c 域名。文件不存在或解析失败返回 None。"""
    try:
        if os.path.exists(_JIN10_Z3C_CACHE_FILE):
            with open(_JIN10_Z3C_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                url = data.get("url")
                if isinstance(url, str) and url.startswith("https://") and ".z3c.jin10.com" in url:
                    return url
    except Exception as e:
        logging.debug(f"读取 z3c 缓存失败: {e}")
    return None


def _save_cached_z3c_url(url: str) -> None:
    """把探测成功的 z3c 域名写入本地缓存。"""
    try:
        os.makedirs(os.path.dirname(_JIN10_Z3C_CACHE_FILE), exist_ok=True)
        with open(_JIN10_Z3C_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"url": url, "ts": time.time()}, f, ensure_ascii=False)
        logging.info(f"已缓存 z3c 域名: {url}")
    except Exception as e:
        logging.warning(f"写入 z3c 缓存失败: {e}")


def _invalidate_z3c_cache() -> None:
    """使用缓存的 URL 请求失败时调用，清除缓存以便下次重新探测。"""
    try:
        if os.path.exists(_JIN10_Z3C_CACHE_FILE):
            os.remove(_JIN10_Z3C_CACHE_FILE)
            logging.info("已清除 z3c 缓存（下次将重新探测）")
    except Exception as e:
        logging.debug(f"清除 z3c 缓存失败: {e}")


def _detect_z3c_base_url() -> str:
    """
    实际探测最新的 z3c 域名（无缓存时调用）。
    策略：从 index.js 抓出所有 32-hex 候选域名，逐个实测 /flash 接口，
          第一个返回 200/400 的就是当前有效的。
    探测成功会写入缓存；探测失败返回硬编码 fallback（不写缓存）。
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0",
        "Referer": "https://www.jin10.com/",
    }
    session = requests.Session()

    try:
        # 1. 抓首页，定位 index.js
        resp_home = session.get("https://www.jin10.com/", headers=headers, timeout=10)
        resp_home.raise_for_status()
        match_js = re.search(r'/new/js/index\.([a-f0-9]+)\.js', resp_home.text)
        if not match_js:
            raise ValueError("首页中未找到 index.js 路径")
        js_url = f"https://www.jin10.com/new/js/index.{match_js.group(1)}.js"
        logging.info(f"获取到 JS 文件: {js_url}")

        # 2. 抓 JS 内容
        resp_js = session.get(js_url, headers=headers, timeout=10)
        resp_js.raise_for_status()
        js_text = resp_js.text

        # 3. 提取所有 32-hex 候选 z3c 域名（去重保序）
        candidates = re.findall(r'([a-f0-9]{32})\.z3c\.jin10\.com', js_text)
        seen, ordered = set(), []
        for h in candidates:
            if h not in seen:
                seen.add(h)
                ordered.append(h)
        if not ordered:
            raise ValueError("JS 中未找到任何 z3c 候选域名")
        logging.info(f"发现 {len(ordered)} 个 z3c 候选，开始探测...")

        # 4. 逐个探测（200=命中，400=后端在但参数错 = 域名有效）
        probe_params = json.dumps(
            {"hot": ["火", "热", "沸", "爆"],
             "max_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "channel": [1, 5]},
            separators=(',', ':'),
        )
        probe_headers = {
            **headers,
            "X-App-Id": "bVBF4FyRTn5NJF5n",
            "X-Version": "1.0",
            "Handleerror": "true",
        }

        for i, h in enumerate(ordered):
            probe_url = f"https://{h}.z3c.jin10.com/flash?params={requests.utils.quote(probe_params)}"
            try:
                r = session.get(probe_url, headers=probe_headers, timeout=6)
                if r.status_code in (200, 400):
                    base_url = f"https://{h}.z3c.jin10.com"
                    logging.info(f"[{i + 1}/{len(ordered)}] 命中: {base_url} (status={r.status_code})")
                    _save_cached_z3c_url(base_url)
                    return base_url
                logging.debug(f"[{i + 1}/{len(ordered)}] {h} 返回 {r.status_code}，跳过")
            except requests.exceptions.RequestException as e:
                logging.debug(f"[{i + 1}/{len(ordered)}] {h} 异常: {e}")
                continue

        raise ValueError(f"所有 {len(ordered)} 个候选 z3c 域名均不可达")

    except Exception as e:
        logging.error(f"动态探测 z3c 域名失败: {e}，使用硬编码 fallback。")
        return _JIN10_FALLBACK_BASE_URL


def get_latest_z3c_base_url(force_refresh: bool = False) -> str:
    """
    获取快讯接口域名。优先使用本地缓存，缓存缺失或 force_refresh=True 时才探测。

    行为：
    - 缓存存在 → 直接返回（零网络开销）
    - 缓存不存在 → 探测 → 成功则写入缓存并返回
    - 探测失败 → 返回硬编码 fallback（不写缓存，下次还会再探测）
    """
    if not force_refresh:
        cached = _load_cached_z3c_url()
        if cached:
            logging.debug(f"使用缓存的 z3c 域名: {cached}")
            return cached
    return _detect_z3c_base_url()


# ---------- 核心抓取函数（带翻页、重试、延时） ----------
# 8/14: 短 TTL 缓存——同一时间窗口几分钟内反复提问不重复分页爬取
_NEWS_CACHE: dict = {}
_NEWS_CACHE_TTL_SECONDS = 180


def fetch_jin10_important_news(start_time_str, end_time_str, base_url=None):
    """
    从金十获取指定时间段内带有热度标签的重要快讯。

    参数:
        start_time_str: 开始时间 "YYYY-MM-DD HH:MM:SS"
        end_time_str:   结束时间 "YYYY-MM-DD HH:MM:SS"
        base_url:       可选，若不提供则自动获取最新域名

    返回:
        list: 重要快讯列表（按时间正序）
    """
    # 命中缓存直接返回（同一场景下连续提问时，新闻内容不变）
    cache_key = (start_time_str, end_time_str, base_url)
    hit = _NEWS_CACHE.get(cache_key)
    if hit and time.time() - hit[0] < _NEWS_CACHE_TTL_SECONDS:
        logging.info(f"[jin10] 命中快讯缓存（{int(time.time() - hit[0])}s 前抓取）")
        return hit[1]

    if base_url is None:
        base_url = get_latest_z3c_base_url()

    api_path = "/flash"
    hot_tags = ["火", "热", "沸", "爆"]
    channel_list = [1, 5]

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate, br",  # 关键修改：不使用 zstd
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": "https://www.jin10.com",
        "Referer": "https://www.jin10.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0",
        "X-App-Id": "bVBF4FyRTn5NJF5n",
        "X-Version": "1.0",
        "Handleerror": "true"
    }

    all_important_news = []
    current_max_time = end_time_str
    target_start_dt = datetime.strptime(start_time_str, "%Y-%m-%d %H:%M:%S")

    page = 1
    cache_refreshed_this_page = False  # 防止同页内无限重探测
    while True:
        # 构造请求 URL
        params_obj = {
            "hot": hot_tags,
            "max_time": current_max_time,
            "channel": channel_list
        }
        params_str = json.dumps(params_obj, separators=(',', ':'))
        url = f"{base_url}{api_path}?params={requests.utils.quote(params_str)}"

        logging.info(f"第 {page} 页请求，max_time = {current_max_time}")

        # 带指数退避的重试（针对 5xx 错误）
        data = None
        for attempt in range(5):
            try:
                resp = requests.get(url, headers=headers, timeout=20)
                if resp.status_code == 502:
                    logging.warning(f"502 Bad Gateway (尝试 {attempt + 1}/5)，等待后重试...")
                    time.sleep(2 ** (attempt + 1))
                    continue
                resp.raise_for_status()
                data = resp.json()
                break
            except requests.exceptions.RequestException as e:
                logging.warning(f"请求异常 (尝试 {attempt + 1}/5): {e}")
                time.sleep(2 ** attempt)
            except json.JSONDecodeError as e:
                logging.warning(f"JSON解析失败 (尝试 {attempt + 1}/5): {e}")
                time.sleep(2)

        # 重试耗尽 → 怀疑缓存的 base_url 已失效，强制重新探测一次
        if data is None and not cache_refreshed_this_page:
            logging.warning("连续5次请求失败，怀疑缓存域名已失效，强制重新探测...")
            _invalidate_z3c_cache()
            new_url = _detect_z3c_base_url()
            if new_url != base_url:
                base_url = new_url
                cache_refreshed_this_page = True
                url = f"{base_url}{api_path}?params={requests.utils.quote(params_str)}"
                logging.info(f"使用新域名重试本页: {base_url}")
                # 用新域名再试一轮
                data = None
                for attempt in range(3):
                    try:
                        resp = requests.get(url, headers=headers, timeout=20)
                        resp.raise_for_status()
                        data = resp.json()
                        break
                    except Exception as e:
                        logging.warning(f"新域名重试 (尝试 {attempt + 1}/3): {e}")
                        time.sleep(2 ** attempt)

        if data is None:
            logging.error("连续5次请求失败，终止抓取。")
            break

        news_list = data.get("data", [])
        if not news_list:
            logging.info("本页无数据，翻页结束。")
            break

        # 过滤：仅保留时间范围内的新闻（该接口已通过 hot 参数过滤热度）
        in_range_news = []
        earliest_time_in_page = None
        for item in news_list:
            time_str = item.get("time")
            if not time_str:
                continue
            try:
                news_dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
            except:
                continue

            if earliest_time_in_page is None or news_dt < earliest_time_in_page:
                earliest_time_in_page = news_dt

            if news_dt < target_start_dt:
                continue

            in_range_news.append(item)

        all_important_news.extend(in_range_news)
        logging.info(
            f"本页获取 {len(news_list)} 条，符合时间范围 {len(in_range_news)} 条，累计 {len(all_important_news)} 条。")

        # 判断是否继续翻页
        if earliest_time_in_page and earliest_time_in_page <= target_start_dt:
            logging.info("已覆盖目标时间范围，停止翻页。")
            break
        if len(news_list) < 30:
            logging.info("返回数据量较少，可能已到末尾。")
            break

        # 准备下一页的 max_time
        if earliest_time_in_page:
            next_max_time = (earliest_time_in_page - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
            current_max_time = next_max_time
        else:
            break

        page += 1
        sleep_sec = random.uniform(1.5, 5.5)
        logging.info(f"等待 {sleep_sec:.2f} 秒后请求下一页...")
        time.sleep(sleep_sec)

    # 按时间正序返回（原始数据是倒序的）
    all_important_news.reverse()
    logging.info(f"总共获取到 {len(all_important_news)} 条重要快讯。")

    # 写入缓存（防膨胀：最多保留 20 个时间窗口）
    _NEWS_CACHE[cache_key] = (time.time(), all_important_news)
    if len(_NEWS_CACHE) > 20:
        oldest_key = min(_NEWS_CACHE, key=lambda k: _NEWS_CACHE[k][0])
        del _NEWS_CACHE[oldest_key]
    return all_important_news


# ---------- 单次分析任务（使用对话历史） ----------
def run_analysis_task(start_time_str: str, end_time_str: str,
                      ai_client, shared_history: list, task_name: str = "",
                      position_prompt: str = "") -> tuple:
    """
    执行一次快讯抓取 + AI 分析，并返回更新后的对话历史和生成内容。
    若提供 position_prompt，则融入提示词（用于手动接口）。
    """
    logging.info(f"[{task_name}] 抓取 {start_time_str} 至 {end_time_str} 的快讯...")
    news_list = fetch_jin10_important_news(start_time_str, end_time_str)

    try:
        with IndexDataFetcher() as fetcher:
            tech_data_text = fetcher.generate_ai_prompt(
                index_name="中证1000",
                periods=["5min", "15min", "30min", "60min", "日线", "周线"]
            )


        # 根据任务类型选择基础 prompt 构建函数
        if task_name == "早间任务":
            sys_prompt, user_prompt = build_morning_prompt(news_list, tech_data_text)
        elif task_name == "午间任务":
            sys_prompt, user_prompt = build_noon_prompt(news_list, tech_data_text)
        elif task_name.startswith("手动报告-"):
            sys_prompt, user_prompt = build_manual_report_prompt(scene=task_name.replace("手动报告-", ""), news_list=news_list, tech_data_text=tech_data_text)
        else:
            # 手动接口自定义场景（如收盘前、收盘后）可在这里扩展
            sys_prompt, user_prompt = build_manual_prompt(
                scene=task_name.replace("手动接口-", ""),
                news_list=news_list,
                tech_data_text=tech_data_text,
                position_prompt=position_prompt
            )

        # 如果有持仓描述，追加到 user_prompt
        if position_prompt:
            user_prompt += f"\n\n**注意：当前用户持仓状态为：{position_prompt}**"

        if task_name.startswith("手动"):
            copy_history = True
        else:
            copy_history = False

        logging.info(f"[{task_name}] 正在调用 AI 生成报告...")
        # 定时任务不需要拷贝历史，直接修改并保存
        summary, updated_history = call_ai_with_history(ai_client, user_prompt, sys_prompt, shared_history, copy_history=copy_history)
        if task_name == "早间任务" or task_name == "午间任务":
            notifycation.send_dingtalk_message(summary)
        logging.info(f"[{task_name}] AI 汇总完成。")
        return summary, updated_history
    except Exception as e:
        logging.error(f"[{task_name}] AI 汇总失败: {e}")
        return "", shared_history

def get_previous_trading_day_15(current_date: datetime) -> datetime:
    """
    返回 current_date 之前最近的一个交易日的 15:00。
    若 current_date 本身是交易日，则返回其前一个交易日。
    """
    # 从昨天开始往前找
    date = current_date - timedelta(days=1)
    while not is_trading_day(date):
        date -= timedelta(days=1)
    return date.replace(hour=15, minute=0, second=0, microsecond=0)


def get_today_9am(now: datetime) -> datetime:
    """返回当天 09:00:00 的 datetime 对象"""
    return datetime(now.year, now.month, now.day, 9, 0, 0)
# ---------- 使用示例 ----------
def main():
    now = datetime.now()
    if not is_trading_day(now):
        logging.info("今日非 A 股交易日，程序退出。")
        return

    ai = llm_client.OpenAICompatibleClient()
    conversation_history = []

    # 加载今日共享历史（可能为空）
    shared_history = load_shared_history()

    # ---------- 早盘分析任务：上午 9:00 执行 ----------
    today_9am = get_today_9am(now)
    if now < today_9am:
        wait_seconds = (today_9am - now).total_seconds()
        logging.info(f"等待至上午 9:00 执行早盘分析（约 {wait_seconds:.0f} 秒）...")
        time.sleep(wait_seconds)
        now = datetime.now()  # 更新当前时间

        # ---------- 早间任务：起始时间为前一个交易日的15:00 ----------
        last_trading_day_15 = get_previous_trading_day_15(now)
        start_early = last_trading_day_15.strftime("%Y-%m-%d %H:%M:%S")
        end_early = now.strftime("%Y-%m-%d %H:%M:%S")
        _, shared_history = run_analysis_task(start_early, end_early, ai, shared_history, task_name="早间任务")
        # 保存历史
        save_shared_history(shared_history)
    else:
        logging.info("已过 9:00，如果需要请调用手动接口")

    # ---------- 收盘前分析任务：下午 3:00 执行 ------------------- 午间任务 ----------
    current_time = now.time()
    noon_time = dt_time(12, 30)
    close_time = dt_time(15, 0)

    if current_time < noon_time:
        wait_seconds = (datetime.combine(now.date(), noon_time) - now).total_seconds()
        logging.info(f"等待至12:30（约 {wait_seconds:.0f} 秒）...")
        time.sleep(wait_seconds)

        start_noon = datetime.now().replace(hour=11, minute=30, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
        end_noon = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        run_analysis_task(start_noon, end_noon, ai, shared_history, task_name="午间任务")
        # 保存历史
        save_shared_history(shared_history)
    elif current_time < close_time:
        # start_noon = now.replace(hour=11, minute=30, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
        # end_noon = now.strftime("%Y-%m-%d %H:%M:%S")
        # run_analysis_task(start_noon, end_noon, ai, shared_history, task_name="午间任务")
        # # 保存历史
        # save_shared_history(shared_history)
        logging.info("已过午盘时间，如果需要请调用手动接口")
    else:
        logging.info("已过收盘时间，跳过午间任务。")

def detect_scene(now: datetime) -> str:
    if not is_trading_day(now):
        return "非交易日"
    t = now.time()
    if t < dt_time(9, 30):
        return "开盘前"
    elif dt_time(11, 30) <= t < dt_time(12, 30):
        return "午盘前"
    elif dt_time(13, 0) <= t < dt_time(15, 0):
        return "收盘前"
    elif t >= dt_time(15, 0):
        return "收盘后"
    else:
        # 交易时段内其他时间，可归为收盘前
        return "收盘前"

def analyze_with_position(position_prompt: str, ai_client=None, scene: str = None) -> str:
    if ai_client is None:
        ai_client = llm_client.OpenAICompatibleClient()

    now = datetime.now()

    # 1. 加载共享历史（只读）
    shared_history = load_shared_history()

    # 2. 确定场景
    if scene is None:
        scene = detect_scene(now)

    # 3. 计算起始和结束时间
    if scene == "开盘前":
        start_time = get_previous_trading_day_15(now)
        end_time = now
    elif scene == "午盘前":
        start_time = now.replace(hour=11, minute=30, second=0, microsecond=0)
        end_time = now
    elif scene == "收盘前":
        start_time = now.replace(hour=11, minute=30, second=0, microsecond=0)  # 或从午盘休市开始
        end_time = now
    elif scene == "收盘后":
        # 交易日15:00之后，起始时间为当日15:00
        start_time = now.replace(hour=15, minute=0, second=0, microsecond=0)
        end_time = now
    elif scene == "非交易日":
        # 非交易日，起始时间为前一个交易日的15:00
        start_time = get_previous_trading_day_15(now)
        end_time = now
    else:
        # 默认按午盘处理
        start_time = now.replace(hour=11, minute=30, second=0, microsecond=0)
        end_time = now

    # 4. 调用分析任务
    summary, _ = run_analysis_task(
        start_time.strftime("%Y-%m-%d %H:%M:%S"),
        end_time.strftime("%Y-%m-%d %H:%M:%S"),
        ai_client,
        shared_history.copy(),
        task_name=f"手动接口-{scene}",
        position_prompt=position_prompt
    )

    return summary

def analyze_without_position(ai_client=None, scene: str = None) -> str:
    """
    生成不带持仓信息的标准市场策略报告（与定时任务输出风格一致）。
    不污染共享历史。
    """
    if ai_client is None:
        ai_client = llm_client.OpenAICompatibleClient()

    now = datetime.now()

    # 1. 加载共享历史（只读）
    shared_history = load_shared_history()

    # 2. 确定场景
    if scene is None:
        scene = detect_scene(now)

    # 3. 计算数据抓取范围（与带持仓接口相同逻辑）
    if scene == "开盘前":
        start_time = get_previous_trading_day_15(now)
        end_time = now
    elif scene == "午盘前":
        start_time = now.replace(hour=11, minute=30, second=0, microsecond=0)
        end_time = now
    elif scene == "收盘前":
        start_time = now.replace(hour=11, minute=30, second=0, microsecond=0)
        end_time = now
    elif scene == "收盘后":
        start_time = now.replace(hour=15, minute=0, second=0, microsecond=0)
        end_time = now
    elif scene == "非交易日":
        start_time = get_previous_trading_day_15(now)
        end_time = now
    else:
        start_time = now.replace(hour=11, minute=30, second=0, microsecond=0)
        end_time = now

    # 4. 调用 run_analysis_task，关键参数：
    #    - position_prompt="" 不传入持仓
    #    - is_manual=False 使用定时任务 Prompt 模板
    #    - 传入 shared_history.copy() 避免污染共享历史
    summary, _ = run_analysis_task(
        start_time.strftime("%Y-%m-%d %H:%M:%S"),
        end_time.strftime("%Y-%m-%d %H:%M:%S"),
        ai_client,
        shared_history.copy(),
        task_name=f"手动报告-{scene}",  # 区别于定时任务的 task_name
        position_prompt=""
    )

    return summary

if __name__ == "__main__":
    main()
    # analyze_with_position("我目前持有中证1000空单2手，成本价8133")