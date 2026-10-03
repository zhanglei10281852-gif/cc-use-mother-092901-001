"""只追加事件存储，支持内存与 JSON 文件两种后端。"""

import json
import threading
from pathlib import Path

from permit_coordination.events import Event, event_from_dict


class EventStore:
    def __init__(self) -> None:
        self._events: list[Event] = []
        self._lock = threading.RLock()
        self._listeners: list = []

    def append(self, event: Event) -> Event:
        with self._lock:
            self._events.append(event)
            for listener in self._listeners:
                listener(event)
        return event

    def all(self) -> tuple[Event, ...]:
        with self._lock:
            return tuple(self._events)

    def add_listener(self, listener) -> None:
        """订阅未来事件（用于实时投影）。"""
        self._listeners.append(listener)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


class InMemoryEventStore(EventStore):
    pass


class JsonFileEventStore(EventStore):
    """事件以 JSON Lines 持久化；启动时重放还原全部状态。"""

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        self._events.append(event_from_dict(json.loads(line)))

    def append(self, event: Event) -> Event:
        super().append(event)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        return event
