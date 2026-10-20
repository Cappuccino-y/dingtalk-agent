"""排入一次「协同重启」——只写标记，**不杀进程**。

背景：在自己这一轮里 Stop-Process 杀监听进程，会把**正在渲染这张卡片的
进程**一起杀掉，钉钉那边卡片就永远停在「正在思考…[AILoading]」。
正确做法是写一个重启标记，让监听进程在
「当前这一轮跑完 + 卡片收尾」之后自行退出，再由看门狗拉起。

用法：
    python tools/bot_restart.py "改了 xxx"     # 排入重启（本轮回答发完后执行）
    python tools/bot_restart.py --status       # 看是否有待重启请求
    python tools/bot_restart.py --cancel       # 撤回待重启请求

注意：重启会清空钉钉侧对话上下文（原设计如此），执行前先跟用户说清楚。
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from tools.opencode_bridge import (  # noqa: E402
    INFLIGHT, RESTART_FLAG, clear_restart, request_restart, restart_request,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="排入一次钉钉机器人协同重启")
    ap.add_argument("reason", nargs="?", default="", help="重启原因（写进日志）")
    ap.add_argument("--status", action="store_true", help="查看是否有待重启请求")
    ap.add_argument("--cancel", action="store_true", help="撤回待重启请求")
    args = ap.parse_args()

    if args.status:
        req = restart_request()
        if req:
            print("有待重启请求：")
            print(json.dumps(req, ensure_ascii=False, indent=2))
        else:
            print("没有待重启请求。")
        return 0

    if args.cancel:
        if restart_request():
            clear_restart()
            print("已撤回待重启请求。")
        else:
            print("本来就没有待重启请求。")
        return 0

    path = request_restart(reason=args.reason or "手动排入", by="bot_restart.py")
    running = INFLIGHT.snapshot()
    print("已排入协同重启（只写了标记，没有杀进程）。")
    print(f"标记文件：{path}")
    if running:
        print(f"当前有 {len(running)} 个回合在跑，等它们跑完 + 卡片收尾后自动重启。")
    else:
        print("当前没有回合在跑，监听进程会在数秒内退出并由看门狗拉起。")
    print("重启后对话上下文会清空；遗留卡片会由新进程自动收尾。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
