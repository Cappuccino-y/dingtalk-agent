"""QVIX 收盘快照作业：每个交易日 15:05 把当天 QVIX 落到本地自建库。

为什么要这个作业：
    optbbs 的日线 k.csv 是「行按日历预建、值事后回填」。正常情况下当晚就回填，
    但 2026-10-23（周五）那次连续两天没回填，50/300/500 三个品种同时空，
    IC 信号直接掉进「拒判」。源站什么时候回填我们说了不算，所以自己存一份：
    收盘后从**当天分钟文件**（vix500.csv，只保留当日 240 根）取 15:00 的最后一个
    有效值，写进 var/qvix/qvix_500etf_daily.csv。读的时候只补空、不覆盖源站值。

用法：
    python jobs/qvix_snapshot.py                # 落当天（要求已收盘）
    python jobs/qvix_snapshot.py --type 500etf  # 只落一个品种
    python jobs/qvix_snapshot.py --date 2026-10-26
    python jobs/qvix_snapshot.py --allow-intraday   # 手动试用，允许盘中值
    python jobs/qvix_snapshot.py --proxy-fill   # 快照拿不到时用官方自算补缺口（晚间那轮）
    python jobs/qvix_snapshot.py --repair 2026-11-02=35.62 --reason "源站算错，双源核对"
    #   ↑ 源站补行但值算错、本地那天又缺（假快照已标 suspect）时，写入核对值；
    #     之后 fetch_qvix 会用它压过源站的错值，缺口补上。--unrepair 2026-11-02 撤销。

    --proxy-fill 说明（2026-11-09）：源站 optbbs 会整天不更新（11-09 分钟文件还是
    11-06 那份），这时 15:05 只能拒落库。晚间官方「期权风险指标」发出来后，用它
    自算 + 锚定链式外推把缺口补上（datalayer/qvix_proxy.py），保证次日 IC 判据
    不因为缺一天而整轮拒判。补出来的行 source=proxy@锚点，源站回填后自动让位。
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for _p in (REPO, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

VAR_DIR = REPO / "var" / "qvix"
LOG_PATH = VAR_DIR / "qvix_snapshot.log"
VAR_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("qvix_snapshot")

# 只关心 IC 信号用的 500ETF，但顺带把另外三个也存了（周报/回溯用得上）
DEFAULT_TYPES = ("500etf", "50etf", "300etf", "1000index")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", action="append", dest="types",
                    help="品种，可重复；默认全部四个")
    ap.add_argument("--date", help="指定日期 YYYY-MM-DD（默认今天）")
    ap.add_argument("--allow-intraday", action="store_true",
                    help="允许盘中值落库（仅手动调试用）")
    ap.add_argument("--repair", metavar="DATE=CLOSE",
                    help="写入经核对的单日值（如 2026-11-02=35.62），不落当天快照")
    ap.add_argument("--unrepair", metavar="DATE", help="撤销某天的核对值")
    ap.add_argument("--proxy-fill", action="store_true",
                    help="快照拿不到时用「官方自算 + 锚定链式」补缺口（晚间那轮用）")
    ap.add_argument("--lookback", type=int, default=10,
                    help="--proxy-fill 最多往回补几个交易日（默认 10）")
    ap.add_argument("--reason", default="", help="--repair 的说明，会写进日志")
    args = ap.parse_args()

    if args.repair or args.unrepair:
        from datalayer.qvix_store import repair_day

        if args.repair:
            try:
                day_s, close_s = args.repair.split("=", 1)
                day, close = date.fromisoformat(day_s.strip()), float(close_s)
            except ValueError:
                log.error("--repair 格式应为 DATE=CLOSE，例如 2026-11-02=35.62")
                return 2
        else:
            day, close = date.fromisoformat(args.unrepair.strip()), None
        types = tuple(args.types) if args.types else ("500etf",)
        for qt in types:
            r = repair_day(qt, day, close, reason=args.reason)
            log.info(f"{qt} {r['date']} {r['action']} close={r['close']}（{r['reason'] or '无说明'}）")
        return 0

    target = date.fromisoformat(args.date) if args.date else datetime.now().date()
    types = tuple(args.types) if args.types else DEFAULT_TYPES

    from datalayer.qvix_store import snapshot

    ok_count, bad = 0, []
    for qt in types:
        try:
            r = snapshot(qt, for_date=target, allow_intraday=args.allow_intraday)
        except Exception as e:  # noqa: BLE001
            log.error(f"{qt} 快照异常: {e}")
            bad.append(f"{qt}:异常")
            continue
        if r.get("ok"):
            ok_count += 1
            log.info(f"{qt} 落库成功 {r['date']} close={r['close']} "
                     f"（分钟行 {r['rows']}，取 {r.get('time')}）")
        else:
            log.warning(f"{qt} 未落库 {r.get('date')}：{r.get('reason')}")
            bad.append(f"{qt}:{r.get('reason')}")

    log.info(f"完成：成功 {ok_count}/{len(types)}；{('未成: ' + '; '.join(bad)) if bad else '全部正常'}")

    # 晚间补采：源站拖到很晚还不更新时，用官方「期权风险指标」自算 + 锚定链式
    # 把缺口补上（2026-11-09）。补上的品种从失败清单里摘掉 —— 数据有了就不该告警，
    # 但日志里留 warning，一眼能看出这一天是代理值而不是实测值。
    if args.proxy_fill and bad:
        from datalayer.qvix_proxy import fill_gaps

        for qt in types:
            if not any(b.startswith(f"{qt}:") for b in bad):
                continue
            try:
                r = fill_gaps(qt, for_date=target, max_lookback=args.lookback)
            except Exception as e:  # noqa: BLE001
                log.error(f"{qt} 代理补齐异常: {e}")
                continue
            if r.get("ok"):
                log.warning(f"{qt} 已用自算代理补 {len(r['written'])} 天："
                            f"{', '.join(r['written'])}（源站回填后会被自动覆盖）")
                bad = [b for b in bad if not b.startswith(f"{qt}:")]
            else:
                log.info(f"{qt} 代理未补：{r.get('reason')}")

        log.info(f"晚间补采后：{('仍有未成 ' + '; '.join(bad)) if bad else '缺口已补上'}")

    # 500ETF 拿不到才算失败（IC 信号就靠它）；其它品种缺了只记日志
    if not any(b.startswith("500etf") for b in bad):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
