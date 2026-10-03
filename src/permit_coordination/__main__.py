"""服务启动入口：``python -m permit_coordination --port 8080``。"""

from __future__ import annotations

import argparse

from .clock import SystemClock
from .event_store import EventStore
from .http_api import ApiContainer, build_server


def main() -> None:
    parser = argparse.ArgumentParser(description="道路测试许可协同后端")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--event-log",
        default="data/events.jsonl",
        help="事件日志文件路径（默认 data/events.jsonl）",
    )
    args = parser.parse_args()

    store = EventStore(args.event_log, clock=SystemClock())
    container = ApiContainer(store, clock=SystemClock())
    server = build_server(args.host, args.port, container)
    print(f"许可协同服务已启动: http://{args.host}:{args.port}  事件日志: {args.event_log}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭服务...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
