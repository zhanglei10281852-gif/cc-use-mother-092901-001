"""只追加的事件存储。

事件是系统唯一的事实来源；状态对象随时可以从事件流重建。
每条事件携带全局递增序号 ``seq``、所属流 ``stream_id``、流内序号、
发生时刻和操作责任人，满足"任一检查时点还原"的取证要求。
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, ValidationError


class Actor:
    """操作人：谁、以什么角色、代表哪个辖区。"""

    def __init__(self, user_id: str, role: str, jurisdiction: str, name: str = ""):
        self.user_id = user_id
        self.role = role
        self.jurisdiction = jurisdiction
        self.name = name

    def to_dict(self) -> dict[str, str]:
        return {
            "user_id": self.user_id,
            "role": self.role,
            "jurisdiction": self.jurisdiction,
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Actor":
        return cls(
            user_id=payload["user_id"],
            role=payload["role"],
            jurisdiction=payload.get("jurisdiction", ""),
            name=payload.get("name", ""),
        )


class Event:
    __slots__ = ("seq", "event_id", "stream_id", "version", "event_type", "at", "actor", "data")

    def __init__(
        self,
        seq: int,
        event_id: str,
        stream_id: str,
        version: int,
        event_type: str,
        at: datetime,
        actor: dict[str, Any],
        data: dict[str, Any],
    ):
        self.seq = seq
        self.event_id = event_id
        self.stream_id = stream_id
        self.version = version
        self.event_type = event_type
        self.at = at
        self.actor = actor
        self.data = data

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "event_id": self.event_id,
            "stream_id": self.stream_id,
            "version": self.version,
            "event_type": self.event_type,
            "at": self.at.isoformat(),
            "actor": self.actor,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Event":
        from .serialization import parse_dt

        return cls(
            seq=payload["seq"],
            event_id=payload["event_id"],
            stream_id=payload["stream_id"],
            version=payload["version"],
            event_type=payload["event_type"],
            at=parse_dt(payload["at"], "at"),
            actor=payload.get("actor", {}),
            data=payload.get("data", {}),
        )


class EventStore:
    """线程安全的内存事件库，可选 JSONL 文件持久化。"""

    def __init__(self, path: str | os.PathLike[str] | None = None, clock: Clock | None = None):
        self._lock = threading.RLock()
        self._events: list[Event] = []
        self._stream_versions: dict[str, int] = {}
        self._clock = clock or SystemClock()
        self.path: Path | None = Path(path) if path else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                self._load()

    # ---- 持久化 -------------------------------------------------------

    def _load(self) -> None:
        assert self.path is not None
        with self.path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = Event.from_dict(json.loads(line))
                except (KeyError, ValueError, json.JSONDecodeError) as exc:
                    raise ValidationError(
                        f"事件日志第 {line_no} 行损坏: {exc}"
                    ) from exc
                if event.seq != len(self._events) + 1:
                    raise ValidationError(
                        f"事件日志第 {line_no} 行全局序号不连续，存储可能被篡改"
                    )
                self._events.append(event)
                self._stream_versions[event.stream_id] = event.version

    def _persist(self, event: Event) -> None:
        if self.path is None:
            return
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

    # ---- 写入 ---------------------------------------------------------

    def append(
        self,
        stream_id: str,
        event_type: str,
        data: dict[str, Any],
        actor: Actor,
        expected_version: int | None = None,
    ) -> Event:
        """向流追加事件。

        expected_version 为调用方读到的流内最新版本号，用于乐观并发控制；
        新流用 ``0`` 表示"必须尚不存在"，``None`` 表示不检查。
        """
        with self._lock:
            return self._append_locked(stream_id, event_type, data, actor, expected_version)

    def transaction(self):
        """跨流决策的全局串行化上下文（如占用检查 + 签发的原子序列）。"""
        return self._lock

    def _append_locked(
        self,
        stream_id: str,
        event_type: str,
        data: dict[str, Any],
        actor: Actor,
        expected_version: int | None,
    ) -> Event:
        current = self._stream_versions.get(stream_id, 0)
        if expected_version is not None and current != expected_version:
            raise ConflictError(
                f"{stream_id} 已被其他操作更新，请刷新后重试",
                {"expected": expected_version, "actual": current},
            )
        next_stream_version = current + 1
        event = Event(
            seq=len(self._events) + 1,
            event_id=f"evt_{uuid.uuid4().hex}",
            stream_id=stream_id,
            version=next_stream_version,
            event_type=event_type,
            at=self._clock.now(),
            actor=actor.to_dict(),
            data=data,
        )
        self._events.append(event)
        self._stream_versions[stream_id] = next_stream_version
        self._persist(event)
        return event

    # ---- 读取 ---------------------------------------------------------

    def stream_version(self, stream_id: str) -> int:
        with self._lock:
            return self._stream_versions.get(stream_id, 0)

    def exists(self, stream_id: str) -> bool:
        return self.stream_version(stream_id) > 0

    def load_stream(
        self,
        stream_id: str,
        as_of: datetime | None = None,
    ) -> list[Event]:
        with self._lock:
            events = [e for e in self._events if e.stream_id == stream_id]
        if not events:
            raise NotFoundError(f"找不到 {stream_id} 对应的事件流")
        if as_of is not None:
            events = [e for e in events if e.at <= as_of]
        return events

    def all_events(self, as_of: datetime | None = None) -> list[Event]:
        with self._lock:
            events = list(self._events)
        if as_of is not None:
            events = [e for e in events if e.at <= as_of]
        return events

    def stream_ids(self, prefix: str | None = None) -> list[str]:
        with self._lock:
            ids = sorted(self._stream_versions, key=lambda sid: self._first_seq(sid))
        if prefix:
            ids = [sid for sid in ids if sid.startswith(prefix)]
        return ids

    def _first_seq(self, stream_id: str) -> int:
        for event in self._events:
            if event.stream_id == stream_id:
                return event.seq
        return 0

    def events_of_type(self, event_type: str, as_of: datetime | None = None) -> list[Event]:
        return [e for e in self.all_events(as_of) if e.event_type == event_type]

    def iter_events(self) -> Iterable[Event]:
        with self._lock:
            return list(self._events)
