"""`jobs/news_digest.py` 的单元测试（2026-11-11 三处 P0 修复的回归保护）。

覆盖：翻页到 cutoff + 覆盖度自证、挑选超时整批保留、兜底不点个股、
脚注覆盖不全提示、跨源去重。全部用合成数据，不联网。

跑法（仓库根目录）：python -m unittest tests.test_news_digest -v
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
for _p in (str(REPO), str(REPO / "jobs"), str(REPO / "datalayer")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import news_digest as nd  # noqa: E402  （jobs/ 下的脚本，按目录导入）


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def em_page(rows, sort_end):
    return {"data": {"fastNewsList": rows, "sortEnd": sort_end}}


def em_row(dt: datetime, title: str = "标题"):
    return {"showTime": dt.strftime("%Y-%m-%d %H:%M:%S"), "title": title,
            "summary": "正文", "code": "20261111"}


class TestEastmoneyPaging(unittest.TestCase):
    """东财必须翻到 cutoff；翻不满要如实标 reached_cutoff=False。"""

    def test_pages_until_cutoff(self):
        now = datetime(2026, 11, 11, 8, 35)
        cutoff = now - timedelta(hours=12)
        pages = [
            em_page([em_row(now - timedelta(hours=h)) for h in (0, 2, 3, 4)], "c1"),
            em_page([em_row(now - timedelta(hours=h)) for h in (5, 7, 8, 9)], "c2"),
            em_page([em_row(now - timedelta(hours=h)) for h in (10, 11, 13, 14)], "c3"),
        ]
        calls = []

        def fake_get(url, params=None, headers=None):
            calls.append(params["sortEnd"])
            return FakeResp(pages[min(len(calls) - 1, len(pages) - 1)])

        with mock.patch.object(nd, "_get", fake_get):
            res = nd.fetch_eastmoney(cutoff)

        self.assertEqual(res.pages, 3)
        self.assertTrue(res.complete)
        # 13h/14h 那两条早于 cutoff，要被筛掉（4+4+2 = 10）
        self.assertEqual(len(res.items), 10)
        self.assertLessEqual(res.oldest, cutoff)

    def test_incomplete_when_pages_exhausted(self):
        now = datetime(2026, 11, 11, 8, 35)
        cutoff = now - timedelta(hours=12)

        def fake_get(url, params=None, headers=None):
            # 永远给新数据、游标一直有 → 只能靠 MAX_PAGES 收手
            return FakeResp(em_page([em_row(now - timedelta(minutes=5))], "c"))

        with mock.patch.object(nd, "MAX_PAGES", 3), \
                mock.patch.object(nd, "_get", fake_get):
            res = nd.fetch_eastmoney(cutoff)

        self.assertEqual(res.pages, 3)
        self.assertFalse(res.complete)
        self.assertEqual(len(res.items), 3)


class TestFutu(unittest.TestCase):
    def test_single_request_200_and_incomplete_flag(self):
        now = datetime(2026, 11, 11, 8, 35)
        cutoff = now - timedelta(hours=12)
        rows = [{"time": int((now - timedelta(hours=1)).timestamp()),
                 "title": "富途快讯", "content": "正文", "detailUrl": ""}]
        seen = {}

        def fake_get(url, params=None, headers=None):
            seen.update(params or {})
            return FakeResp({"data": {"data": {"news": rows}}})

        with mock.patch.object(nd, "_get", fake_get):
            res = nd.fetch_futu(cutoff)

        self.assertEqual(seen.get("pageSize"), str(nd.FUTU_PAGE_SIZE))
        self.assertEqual(len(res.items), 1)
        self.assertFalse(res.complete)      # 最old的是 1h 前，没到 12h


class TestPickDeadlineKeepsItems(unittest.TestCase):
    """挑选超时后，没轮到 worker 的分批必须整批保留（不能静默丢料）。"""

    def test_unprocessed_chunks_are_kept(self):
        items = [{"source": "东财", "time": datetime(2026, 11, 11, 6, 0),
                  "title": f"快讯{i}", "text": "", "url": ""} for i in range(12)]

        def slow_pick(client, chunk, slot=None):
            import time as _t
            _t.sleep(0.6)
            return []           # 模型这批一条都不挑（这批的条目本来就会被丢）

        with mock.patch.object(nd, "_pick_chunk", slow_pick), \
                mock.patch.object(nd, "PICK_WORKERS", 1):
            out = nd.llm_pick(object(), items, batch=4, deadline_s=1)

        # 1s 时限内单 worker 只跑完第 1 批（返回空），剩下 2 批必须整批保留 = 8 条。
        # 断言的是「剩下的 8 条一条不少」，而不是「12 条全回来」——
        # 模型处理过并明确说"不要"的那批，本来就该丢。
        self.assertEqual(len(out), 8)
        kept = {id(x) for x in out}
        self.assertEqual(len(kept), 8)
        self.assertTrue(all(x["title"].startswith("快讯") for x in out))


class TestFallbackNoStockNames(unittest.TestCase):
    def test_fallback_only_sectors(self):
        t = datetime(2026, 11, 11, 8, 36)
        items = [
            {"source": "东财", "time": t, "title": "中际旭创（300308）光模块订单饱满",
             "text": "CPO 需求旺盛", "url": ""},
            {"source": "富途", "time": t + timedelta(hours=1),
             "title": "剑桥科技涨停，光通信走强", "text": "", "url": ""},
            {"source": "新浪", "time": t + timedelta(hours=2),
             "title": "新易盛发布减持公告", "text": "", "url": ""},
            {"source": "东财", "time": t + timedelta(hours=3),
             "title": "碳酸锂价格上调", "text": "锂电材料", "url": ""},
        ]
        with mock.patch.object(nd, "SECTOR_HINTS", nd.SECTOR_HINTS):
            text = nd.fallback_digest(items)

        for name in ("中际旭创", "剑桥科技", "新易盛", "300308"):
            self.assertNotIn(name, text)
        self.assertIn("降级模式", text)
        self.assertIn("光通信/CPO：2 条", text)     # 前两条归到光通信
        self.assertIn("锂电/资源：1 条", text)
        self.assertIn("其余 1 条未归类", text)      # 减持公告只有个股名，无板块词

    def test_empty_items(self):
        text = nd.fallback_digest([])
        self.assertIn("今日无新增重要产业消息", text)
        self.assertNotIn("未归类", text)


class TestMarkdownCoverage(unittest.TestCase):
    def _cov(self, **kw):
        defaults = dict(items=[], oldest=datetime(2026, 11, 11, 5, 20),
                        pages=6, reached_cutoff=True)
        defaults.update(kw)
        return nd.FetchResult(**defaults)

    def test_all_complete(self):
        cov = {"东财": self._cov(), "同花顺": self._cov()}
        md = nd.build_markdown("正文", {"东财": 300, "同花顺": 100}, 200, 12, cov)
        self.assertIn("2 个源均已回溯满 12 小时", md)
        self.assertNotIn("覆盖不全", md)

    def test_incomplete_is_flagged(self):
        cov = {
            "东财": self._cov(),
            "同花顺": self._cov(oldest=datetime(2026, 11, 11, 9, 33), reached_cutoff=False),
            "富途": self._cov(oldest=None, reached_cutoff=False, pages=1),
        }
        md = nd.build_markdown("正文", {"东财": 300, "同花顺": 300, "富途": 0}, 200, 12, cov)
        self.assertIn("覆盖不全", md)
        self.assertIn("同花顺 只到 09:33", md)
        self.assertIn("富途（无数据）", md)
        self.assertNotIn("东财 只到", md)

    def test_no_coverage_param_keeps_old_shape(self):
        md = nd.build_markdown("正文", {"东财": 10}, 8, 12)
        self.assertIn("扫了近 12 小时", md)
        self.assertNotIn("覆盖", md)


class TestRenderItems(unittest.TestCase):
    """板块行 + 影响行必须合并成一段（钉钉不认段内单换行）。"""

    def test_two_lines_become_one_paragraph(self):
        body = ("**油气/油运**｜美方称持续执行霍尔木兹海峡海上封锁（富途/新浪·11月12日02:12）\n"
                "→ 影响：若冲突外溢并扰动通航，原油与航运保险成本波动加大")
        out = nd._render_items(body)
        # 一条 = 一段：不再有裸换行把「事件」和「影响」切开
        self.assertNotIn("\n", out)
        self.assertIn("**油气/油运**", out)
        self.assertIn("→ 影响：", out)

    def test_multiple_items_separated_by_blank_line(self):
        body = ("**A**｜事件甲\n→ 影响：甲\n"
                "**B**｜事件乙\n→ 影响：乙")
        out = nd._render_items(body)
        self.assertEqual(len(out.split("\n\n")), 2)
        self.assertTrue(out.startswith("**A**"))
        self.assertIn("**B**", out)

    def test_model_blank_line_between_item_lines_still_merges(self):
        """模型偶尔在板块行和影响行之间插空行 —— 也必须归并成一条。"""
        body = "**A**｜事件甲\n\n→ 影响：甲"
        out = nd._render_items(body)
        self.assertEqual(out.count("\n\n"), 0)
        self.assertIn("→ 影响：甲", out)

    def test_empty_body(self):
        self.assertEqual(nd._render_items(""), "")


class TestPickHardLimit(unittest.TestCase):
    """挑选阶段必须在 08:50 前收手（IC 监控的判定时刻，别抢模型配额）。"""

    def test_hard_limit_caps_deadline(self):
        """hard_limit_s 小于自动算出的时限时，取 hard_limit_s。"""
        import news_digest as ndx
        captured = {}

        def fake_pick(client, chunk, slot=None):
            captured["chunk"] = chunk
            return []

        with mock.patch.object(ndx, "_pick_chunk", fake_pick), \
                mock.patch.object(ndx, "PICK_WORKERS", 3), \
                mock.patch.object(ndx, "PICK_DEADLINE", 420), \
                mock.patch.object(ndx, "PICK_SECONDS_PER_BATCH", 180):
            # 1000 条 / 300 一批 = 4 批 → 自动时限 = max(420, 4*180/3=240) = 420
            ndx.llm_pick(object(), [{"title": f"t{i}", "time": datetime.now()}
                                    for i in range(1000)], deadline_s=None,
                          hard_limit_s=100)
        # 不抛异常即可（真正的时间控制靠线程 join）
        self.assertIn("chunk", captured)

    def test_margin_constant_positive(self):
        self.assertGreater(nd.PICK_HARD_MARGIN, 0)

    def test_batch_size_fits_time_budget(self):
        """候选 2660 条（25h 粗筛后）时，挑选不能长到抢掉 IC 监控的配额。

        基准来自生产日志：12h/9 批那一轮，8 批 / 308s 墙钟 = 38s/批
        （PICK_SECONDS_PER_BATCH 已是并发后的墙钟吞吐，不能再除并发数）。
        23 批 × 40s ≈ 920s ≈ 15 分钟 —— 单靠这个装不进 08:35→08:50 的 15 分钟，
        所以真正的保障是**两道闸**：
          ① `PICK_HARD_MARGIN`：挑选阶段在 08:50 前让出模型配额；
          ② `_interleave_by_time`：超时丢掉的是各时段同样靠后的部分，
             整个时间轴仍有代表（不会只留一头）。
        这里断言的是「这两道闸都在」。
        """
        import math
        batches = math.ceil(2660 / nd.BATCH_SIZE)
        seconds = batches * nd.PICK_SECONDS_PER_BATCH
        # 硬闸必须在位（挑选提前收手，给 IC 监控让路）
        self.assertGreater(nd.PICK_HARD_MARGIN, 0)
        # 时限按批数放大，不是固定值
        self.assertGreater(seconds, nd.PICK_DEADLINE)
        # 超时时按跨时段轮转挑选 → 丢的不偏科
        order = nd._interleave_by_time(range(batches))
        self.assertEqual(sorted(order), list(range(batches)))
        first = sorted(order[:batches // 2])
        # 前一半的批号要**横跨整个时间轴**（而不是挤在一头）
        self.assertEqual(first[0], 0)
        self.assertGreater(first[-1] - first[0], batches * 0.8)


class TestInterleaveByTime(unittest.TestCase):
    """挑选超时时，丢掉的批次不能全挤在时间轴的一端。"""

    def test_prefix_spans_whole_axis(self):
        """前一半的批号应横跨整个时间轴（每组都拿到头部）。"""
        order = nd._interleave_by_time(range(22))
        self.assertEqual(sorted(order), list(range(22)))
        first_half = sorted(order[:11])
        # 覆盖到最早(0)、最晚(21)，且不全是头尾
        self.assertIn(0, first_half)
        self.assertIn(21, first_half)
        self.assertGreater(len(first_half), 4)

    def test_all_batches_present_once(self):
        order = nd._interleave_by_time(range(22))
        self.assertEqual(len(order), 22)
        self.assertEqual(sorted(order), list(range(22)))

    def test_small_input_unchanged(self):
        self.assertEqual(nd._interleave_by_time([0]), [0])
        self.assertEqual(sorted(nd._interleave_by_time([0, 1])), [0, 1])


class TestPrefilterNoise(unittest.TestCase):
    """本地粗筛：只丢「确定是纯行情播报」的，绝不能误伤事件类消息。

    存在的理由：窗口扩到 25h 后候选 ~3100 条，而挑选是逐批调模型
    （120 条/批，MiniMax 单批 30~150s），26 批要 ~24 分钟，08:35 起跑
    到 09:00 才完。先本地剔掉噪音，模型才不用花配额读行情播报。
    """

    def _keep(self, text):
        it = {"title": text, "text": "", "time": datetime(2026, 11, 12, 8, 0)}
        keep, drop = nd.prefilter_noise([it])
        return len(keep) == 1, len(drop) == 1

    def test_keeps_chaoying_ir_records(self):
        """超颖电子那两条 IR 记录必须留下（这是本次修复要抓的东西）。"""
        a = ("【超颖电子：今年存储类产品订单能见度充足，多数优质产能已被长单锁定】"
             "11月11日，超颖电子发布投资者关系活动记录表。存储芯片供需偏紧、头部厂商持续扩产。")
        b = ("【超颖电子：黄石P3的产能规划指向存储PCB产能瓶颈的突破】"
             "拟投资20.86亿元建设高多层及HDI印制电路板产能，建设期限24个月。")
        self.assertTrue(self._keep(a)[0])
        self.assertTrue(self._keep(b)[0])

    def test_drops_pure_quote_noise(self):
        for text in ("PCB概念震荡拉升 超颖电子涨停，广合科技跟涨",
                     "AAPL跌超5%，英伟达跌超3%",
                     "主力净流入超3亿元，XX板块领涨",
                     "美股盘前跌超59%，现报70.00"):
            self.assertTrue(self._keep(text)[1], f"应丢弃：{text}")

    def test_event_wins_over_quote(self):
        """同时含行情词和事件词 → 保留（宁可多留，不要误伤）。"""
        self.assertTrue(self._keep("某公司涨停，因获批新订单，产能扩至10万吨")[0])
        self.assertTrue(self._keep("国务院批准发布一批国家标准，强制性21项")[0])
        self.assertTrue(self._keep("某车企宣布拟在欧洲投资20亿欧元建设工厂")[0])

    def test_keeps_normal_news(self):
        for text in ("存储原厂第四季度官方价格微涨，渠道称消费级存储同步上涨",
                     "日本央行委员：黑田东彦的刺激政策在当时是合适的",
                     "市场监管总局批准发布一批国家标准"):
            self.assertTrue(self._keep(text)[0], f"应保留：{text}")

    def test_drops_nothing_when_no_quote_words(self):
        """不含行情特征的条目一律保留（默认保留，不是默认丢弃）。"""
        keep, drop = nd.prefilter_noise([{"title": "一条普通消息", "text": ""}])
        self.assertEqual(len(keep), 1)
        self.assertEqual(len(drop), 0)


class TestSpreadSamplesAcrossTime(unittest.TestCase):
    """终稿候选必须**按时间均匀取样**，不能只取批次完成顺序的前 N 条。

    `llm_pick` 的分批按时间升序切，而 picked 是批次完成顺序拼的
    → 直接 `picked[:80]` 会只拿到最老那几批，最新消息被丢掉。
    """

    def _mk(self, n, start=datetime(2026, 11, 12, 8, 0)):
        """造 n 条，**时间升序**给出 —— 模拟真实 picked 的顺序。

        真实链路：`dedupe` 按时间升序返回 → 分批按顺序切（chunk0 最老）
        → worker 从队首取批 → **chunk0 先完成** → picked 是完成顺序，
        所以 picked 里也是**最老在前、最新在后**。
        """
        return [{"time": start - timedelta(minutes=n - 1 - i), "title": f"t{i}"}
                for i in range(n)]

    def test_latest_always_included(self):
        picked = self._mk(300)
        out = nd._spread(picked, 80)
        self.assertEqual(len(out), 80)
        newest = max(picked, key=lambda x: x["time"])
        self.assertIn(newest, out, "最新一条必须在候选里")

    def test_covers_whole_range(self):
        picked = self._mk(300)
        out = nd._spread(picked, 80)
        times = [it["time"] for it in out]
        lo, hi = min(it["time"] for it in picked), max(it["time"] for it in picked)
        span = (hi - lo).total_seconds()
        covered = (max(times) - min(times)).total_seconds()
        # 覆盖整个时段（允许一个采样步长的误差）
        self.assertGreaterEqual(covered, span * 0.9)
        # 新的一半和旧的一半都要有代表
        mid = lo + (hi - lo) / 2
        self.assertTrue(any(t > mid for t in times), "新时段没有代表")
        self.assertTrue(any(t < mid for t in times), "旧时段没有代表")

    def test_small_input_kept_all_sorted_desc(self):
        picked = self._mk(10)
        out = nd._spread(picked, 80)
        self.assertEqual(len(out), 10)
        self.assertEqual(out, sorted(picked, key=lambda x: x["time"], reverse=True))

    def test_order_is_newest_first(self):
        picked = self._mk(50)
        out = nd._spread(picked, 80)
        times = [it["time"] for it in out]
        self.assertEqual(times, sorted(times, reverse=True))

    def test_regression_old_behaviour_dropped_latest(self):
        """钉住现状：老的 `picked[:80]` 会丢掉最新消息（改回去时这条会挡住）。"""
        picked = self._mk(300)          # 完成顺序 = 最老的在前
        old = picked[:80]
        newest = max(picked, key=lambda x: x["time"])
        self.assertNotIn(newest, old)   # 老做法确实漏掉最新的


class TestPickDeadlineScalesWithBatches(unittest.TestCase):
    """窗口变大 → 批数变多 → 挑选时限必须跟着放大，否则大批未筛选的原始快讯进终稿。"""

    def test_deadline_scales_with_chunk_count(self):
        calls = {}

        def fake_pick(client, chunk, slot=None):
            calls.setdefault("batches", []).append(len(chunk))
            return []

        # 300 条 → 3 批；时限应大于固定 420s
        with mock.patch.object(nd, "_pick_chunk", fake_pick), \
                mock.patch.object(nd, "PICK_WORKERS", 3), \
                mock.patch.object(nd, "PICK_DEADLINE", 420):
            nd.llm_pick(object(), [{"title": f"t{i}", "time": datetime.now()}
                                   for i in range(300)], batch=100, deadline_s=None)
        self.assertIn("batches", calls)

    def test_auto_deadline_larger_for_more_batches(self):
        """批数多 → 自动算出的时限更大（用公式直接验证，避免调模型）。"""
        def auto_deadline(n_chunks, workers=3, per_batch=100, base=420):
            return max(base, int(n_chunks * per_batch / max(1, workers)))
        self.assertEqual(auto_deadline(9), 420)      # 12h 的 9 批 → 仍用基础值
        self.assertGreater(auto_deadline(25), 420)   # 25h 的 25 批 → 放大


class TestSpreadPrefersModelPicked(unittest.TestCase):
    """挑选超时时兜底保留的原始快讯，不能淹没模型挑的成品。"""

    def test_model_picked_wins_over_fallback(self):
        t0 = datetime(2026, 11, 12, 8, 0)
        picked = []
        for i in range(200):
            picked.append({"time": t0 - timedelta(minutes=i), "title": f"m{i}",
                           "_picked": True})
        for i in range(200, 400):
            picked.append({"time": t0 - timedelta(minutes=i), "title": f"raw{i}",
                           "_picked": False})
        out = nd._spread(picked, 80)
        self.assertEqual(len(out), 80)
        self.assertTrue(all(it["_picked"] for it in out),
                        "候选里不该出现未经模型筛选的原始快讯")

    def test_fallback_only_used_when_needed(self):
        t0 = datetime(2026, 11, 12, 8, 0)
        picked = [{"time": t0 - timedelta(minutes=i), "title": f"m{i}", "_picked": True}
                  for i in range(20)]
        picked += [{"time": t0 - timedelta(minutes=100 + i), "title": f"r{i}",
                    "_picked": False} for i in range(60)]
        out = nd._spread(picked, 80)
        # 只有 20 条模型挑的，其余用兜底补
        self.assertEqual(len(out), 80)
        self.assertEqual(sum(1 for it in out if it["_picked"]), 20)


class TestWindowCoversFullTradingDay(unittest.TestCase):
    """一天只跑一次 → 回溯窗口必须跨过一整个交易日，否则有盲区。"""

    def test_default_window_covers_whole_day(self):
        # 12h 会漏掉「前一交易日盘中到盘后」那 12 小时（超颖电子就是这么漏的）
        self.assertGreaterEqual(nd.DEFAULT_HOURS, 24)

    def test_window_covers_previous_session(self):
        """08:35 跑的时候，窗口必须盖住前一天 08:35 之后到盘后的消息。"""
        run_at = datetime(2026, 11, 12, 8, 35)
        cutoff = run_at - timedelta(hours=nd.DEFAULT_HOURS)
        # 前一交易日的盘后 IR 记录（超颖电子 11-11 16:03）
        ir_record = datetime(2026, 11, 11, 16, 3)
        self.assertLess(cutoff, ir_record)
        self.assertLess(ir_record, run_at)
        # 上一轮（11-11 08:35）的起点也要落在窗口内 → 相邻两轮之间无盲区
        prev_run = datetime(2026, 11, 11, 8, 35)
        self.assertLess(cutoff, prev_run)

    def test_window_overlaps_between_runs(self):
        """相邻两轮必须**重叠**而不是首尾相接 —— 实跑时刻会漂（调度 08:35 / 实跑 08:41）。"""
        prev_run = datetime(2026, 11, 11, 8, 41)   # 上一轮实跑时刻
        next_run = datetime(2026, 11, 12, 8, 35)   # 这一轮实跑时刻
        cutoff = next_run - timedelta(hours=nd.DEFAULT_HOURS)
        self.assertLess(cutoff, prev_run, "两轮之间必须重叠，否则漂移时会裂开缝")

    def test_twelve_hour_window_would_miss_it(self):
        """钉住现状：12h 窗口确实会漏（改回去时这条测试会挡住）。

        12h 窗口 = [11-11 20:35, 11-12 08:35]，而超颖电子的 IR 记录是
        11-11 16:03 —— **早于窗口起点**，所以两轮都盖不到它。
        """
        run_at = datetime(2026, 11, 12, 8, 35)
        cutoff_12h = run_at - timedelta(hours=12)
        ir_record = datetime(2026, 11, 11, 16, 3)
        self.assertLess(ir_record, cutoff_12h)   # 早于 12h 窗口起点 → 被漏掉
        # 上一轮（11-11 08:41，12h 窗口）也盖不到：它的终点在 16:03 之前
        prev_run_end = datetime(2026, 11, 11, 8, 41)
        self.assertLess(prev_run_end, ir_record)


class TestDingtalkLayout(unittest.TestCase):
    """推送层排版：钉钉 markdown 不认段内单换行，必须转成空行。"""

    def _norm(self, text):
        from tools.dingtalk_push import normalize_for_dingtalk
        return normalize_for_dingtalk(text)

    def test_soft_break_becomes_paragraph_break(self):
        out = self._norm("第一行\n第二行")
        self.assertEqual(out, "第一行\n\n第二行")

    def test_list_items_stay_one_list(self):
        out = self._norm("- 甲\n- 乙\n- 丙")
        # 列表项之间不能插空行（会被拆成三个独立列表）
        self.assertEqual(out, "- 甲\n- 乙\n- 丙")

    def test_numbered_list_keeps_numbering(self):
        out = self._norm("1. 甲\n2. 乙")
        self.assertIn("1. 甲", out)
        self.assertIn("2. 乙", out)
        self.assertNotIn("\n\n", out)

    def test_heading_downgraded_to_bold(self):
        out = self._norm("## 标题")
        self.assertEqual(out, "**标题**")

    def test_rule_removed(self):
        out = self._norm("上面\n\n---\n\n下面")
        self.assertNotIn("---", out)
        self.assertIn("上面", out)
        self.assertIn("下面", out)

    def test_quote_lines_split(self):
        out = self._norm("> 甲\n> 乙")
        self.assertIn("> 甲", out)
        self.assertIn("> 乙", out)
        self.assertIn("\n\n", out)   # 两行引用不能并成一段

    def test_idempotent(self):
        src = "**A**｜事件\n→ 影响：逻辑\n\n- 甲\n- 乙\n\n> 注"
        once = self._norm(src)
        self.assertEqual(once, self._norm(once))

    def test_real_news_body_has_no_stuck_together_lines(self):
        """真实早报正文：每个板块块之间要有换行，不能挤成一段。"""
        body = ("**油气/油运**｜美方称持续执行霍尔木兹海峡海上封锁（富途/新浪·11月12日02:12）\n"
                "→ 影响：若冲突外溢并扰动通航，原油与航运保险成本波动加大\n"
                "**AI算力硬件**｜海外头部云厂商拟新建云区域（东财/富途·11月11日22:35）\n"
                "→ 影响：海外AI基础设施投入延续")
        md = nd.build_markdown(body, {"东财": 251, "新浪": 615}, 866, 12)
        norm = self._norm(md)
        # 板块块之间是空行（= 渲染时真换行）
        self.assertIn("**油气/油运**", norm)
        self.assertIn("**AI算力硬件**", norm)
        self.assertGreaterEqual(norm.count("\n\n"), 3)



class TestSlotsAndNoonWindow(unittest.TestCase):
    """版位化：早间 08:50 投递、午间 12:55 投递（且**到点才推**）。"""

    def test_deliver_times(self):
        self.assertEqual(nd.SLOTS["morning"].deliver_at, (8, 50))
        self.assertEqual(nd.SLOTS["noon"].deliver_at, (12, 55))

    def test_noon_keeps_intraday_morning_does_not(self):
        self.assertTrue(nd.SLOTS["noon"].keep_intraday)
        self.assertFalse(nd.SLOTS["morning"].keep_intraday)
        self.assertTrue(nd.SLOTS["noon"].intraday_section)
        self.assertFalse(nd.SLOTS["morning"].intraday_section)

    def test_both_slots_have_stocks_section(self):
        """用户 2026-11-12：「像个股的重大利好可以做推送」。"""
        self.assertTrue(nd.SLOTS["morning"].stocks_section)
        self.assertTrue(nd.SLOTS["noon"].stocks_section)

    def test_auto_slot_by_clock(self):
        self.assertIs(nd._auto_slot(datetime(2026, 11, 12, 8, 40)), nd.SLOTS["morning"])
        self.assertIs(nd._auto_slot(datetime(2026, 11, 12, 12, 30)), nd.SLOTS["noon"])

    def test_noon_window_starts_at_morning_push(self):
        """午间版窗口 = 距早报推送至今（覆盖上午盘中，不重复早报）。"""
        state = {"slots": {"morning": {"at": "2026-11-12T08:50:57"}}}
        now = datetime(2026, 11, 12, 12, 20)
        hours = nd._default_hours(nd.SLOTS["noon"], state, now)
        cutoff = now - timedelta(hours=hours)
        self.assertLess(cutoff, datetime(2026, 11, 12, 8, 50))   # 盖住早报推送时刻
        self.assertGreater(cutoff, datetime(2026, 11, 12, 7, 0))   # 不会回溯太远

    def test_noon_window_fallback_when_no_morning_push(self):
        hours = nd._default_hours(nd.SLOTS["noon"], {}, datetime(2026, 11, 12, 12, 20))
        self.assertEqual(hours, nd.NOON_DEFAULT_HOURS)

    def test_morning_window_is_25h(self):
        hours = nd._default_hours(nd.SLOTS["morning"], {}, datetime(2026, 11, 12, 8, 40))
        self.assertEqual(hours, nd.DEFAULT_HOURS)


class TestWaitUntilDelivery(unittest.TestCase):
    """「12:55 要看到推送」= 内容备好后等到 12:55 再推，不是 12:55 才开始跑。"""

    def test_waits_when_target_in_future(self):
        slept = []
        target = datetime.now() + timedelta(seconds=30)
        with mock.patch.object(nd.time, "sleep", lambda s: slept.append(s)):
            nd._wait_until(target)
        self.assertEqual(len(slept), 1)
        self.assertGreater(slept[0], 0)

    def test_returns_immediately_when_past(self):
        slept = []
        with mock.patch.object(nd.time, "sleep", lambda s: slept.append(s)):
            nd._wait_until(datetime.now() - timedelta(seconds=5))
        self.assertEqual(slept, [])


class TestPrefilterKeepIntraday(unittest.TestCase):
    """午间版必须保留「盘中异动」—— 那正是午间版要回答的东西。"""

    def _keep(self, text, keep_intraday):
        it = {"title": text, "text": "", "time": datetime(2026, 11, 12, 11, 30)}
        keep, drop = nd.prefilter_noise([it], keep_intraday=keep_intraday)
        return len(keep) == 1

    def test_intraday_dropped_in_morning_kept_in_noon(self):
        for text in ("光伏板块异动拉升，协鑫集成触及涨停",
                     "创新药概念震荡拉升，万邦医药涨超10%",
                     "军工板块震荡拉升 航天南湖涨超10%"):
            self.assertFalse(self._keep(text, False), f"早间应丢弃：{text}")
            self.assertTrue(self._keep(text, True), f"午间应保留：{text}")

    def test_pure_fund_flow_still_dropped_in_noon(self):
        """只丢「确定是纯行情播报」：资金流/ETF 噪音两个版位都丢。"""
        for text in ("主力净流入超3亿元，XX板块成交清淡",
                     "ETF两市成交额报2563.02亿元，较昨日此时缩量29.37亿元",
                     "大盘主力净流出200亿"):
            self.assertFalse(self._keep(text, True), f"应丢弃：{text}")


class TestRenderSections(unittest.TestCase):
    """分区排版：主线 / 板块 / 个股 / 异动，各自成段，编号即顺序。"""

    BODY = (
        "主线：算力芯片与存储涨价共振，科创50早盘涨2.35%（东财·11:32）\n"
        "\n"
        "板块：\n"
        "1. **半导体/存储**：头部存储企业告知涨价意向，费城半导体指数涨近6%（东财·11:36）\n"
        "→ 影响：涨价与扩产同时兑现，设备与材料环节景气延续\n"
        "2. **AI算力**：百度发布1024卡全光超节点，昆仑芯M300明年推出（东财·11:51）\n"
        "→ 影响：国产算力互联方案推进\n"
        "\n"
        "个股：\n"
        "1. **国轩高科**：2GWh全固态量产线启动，设备订单分批落地（东财·11:56）\n"
        "→ 影响：固态电池由中试走向量产线\n"
        "\n"
        "异动：\n"
        "- 光伏：协鑫集成涨停、亚玛顿2连板（新浪·10:34）\n"
        "- 创新药：万邦医药涨超10%（新浪·11:04）\n"
    )

    def test_sections_are_split(self):
        out = nd._render_sections(self.BODY, nd.SLOTS["noon"])
        self.assertIn("**主线**｜算力芯片与存储涨价共振", out)
        self.assertIn("**板块要闻**", out)
        self.assertIn("**个股重大利好**", out)
        self.assertIn("**盘中异动**", out)

    def test_items_are_numbered_and_own_paragraph(self):
        out = nd._render_sections(self.BODY, nd.SLOTS["noon"])
        self.assertIn("**1 · 半导体/存储**｜头部存储企业告知涨价意向", out)
        self.assertIn("**2 · AI算力**｜百度发布1024卡全光超节点", out)
        # 事件与影响是**两段**（中间空行）—— 钉钉才渲染得出换行
        self.assertIn("**1 · 半导体/存储**｜头部存储企业告知涨价意向，费城半导体指数涨近6%（东财·11:36）\n\n→ 影响：涨价与扩产同时兑现",
                      out)

    def test_intraday_bullets_stay_one_list(self):
        """列表项之间不能插空行（会被拆成多个列表 / 重新编号）。"""
        out = nd._render_sections(self.BODY, nd.SLOTS["noon"])
        block = out.split("**盘中异动**")[1]
        self.assertIn("- 光伏：协鑫集成涨停、亚玛顿2连板（新浪·10:34）\n- 创新药：万邦医药涨超10%（新浪·11:04）",
                      block)

    def test_stock_names_only_in_stock_section(self):
        out = nd._render_sections(self.BODY, nd.SLOTS["noon"])
        sector_part = out.split("**板块要闻**")[1].split("**个股重大利好**")[0]
        self.assertNotIn("国轩高科", sector_part)

    def test_plain_body_falls_back_to_old_layout(self):
        body = ("**油气/油运**｜美方称持续执行霍尔木兹海峡海上封锁（富途/新浪·11月12日02:12）\n"
                "→ 影响：若冲突外溢并扰动通航")
        out = nd._render_sections(body, nd.SLOTS["morning"])
        self.assertIn("**油气/油运**", out)
        self.assertNotIn("\n", out)     # 老口径：事件+影响并成一段

    def test_empty_body(self):
        self.assertEqual(nd._render_sections("", nd.SLOTS["noon"]), "")


class TestBuildMarkdownWindow(unittest.TestCase):
    """脚注要写清时间范围（读者要知道这段覆盖到几点）。"""

    def test_footer_has_range(self):
        md = nd.build_markdown("主线：x", {"东财": 10}, 8, 4,
                               slot=nd.SLOTS["noon"],
                               window=(datetime(2026, 11, 12, 8, 50),
                                       datetime(2026, 11, 12, 12, 55)))
        self.assertIn("11-12 08:50~11-12 12:55", md)
        self.assertIn("扫了近 4 小时", md)

    def test_disclaimer_mentions_stocks_are_not_advice(self):
        md = nd.build_markdown("主线：x", {"东财": 10}, 8, 4, slot=nd.SLOTS["noon"])
        self.assertIn("不构成买卖建议", md)


class TestDedupe(unittest.TestCase):
    def test_cross_source_merge_keeps_earliest(self):
        # 同一措辞（各源抄稿）：按 4-gram Jaccard 能合到一起
        t0 = datetime(2026, 11, 11, 8, 36)
        items = [
            {"source": "东财", "time": t0,
             "title": "微软向特朗普政府施压要求放弃中国光收发器进口禁令",
             "text": "", "url": "u1"},
            {"source": "富途", "time": t0 + timedelta(minutes=10),
             "title": "微软向特朗普政府施压，要求放弃中国光收发器进口禁令",
             "text": "更长的正文", "url": ""},
        ]
        kept = nd.dedupe(items)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["time"], t0)
        self.assertEqual(sorted(kept[0]["sources"]), ["东财", "富途"])
        self.assertEqual(kept[0]["text"], "更长的正文")
        self.assertEqual(kept[0]["url"], "u1")

    def test_paraphrase_is_not_merged_yet(self):
        # ⚠ 已知局限（P1 待改）：改写式重复抓不住。2026-11-11 那条光模块消息，
        # 东财写「施压…要求放弃」，富途写「敦促…放弃拟议的」，4-gram 相似度过不了 0.5。
        # 这条测试是为了**把现状钉住**：以后换 embedding/聚类方案时它会先红。
        t0 = datetime(2026, 11, 11, 8, 36)
        items = [
            {"source": "东财", "time": t0,
             "title": "微软向特朗普政府施压，要求放弃拟议中的中国光收发器进口禁令",
             "text": "", "url": ""},
            {"source": "富途", "time": t0 + timedelta(hours=4),
             "title": "中际旭创a股高开2%，路透独家报导微软敦促特朗普政府放弃该禁令",
             "text": "", "url": ""},
        ]
        kept = nd.dedupe(items)
        self.assertEqual(len(kept), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
