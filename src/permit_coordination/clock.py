"""可注入的时钟，测试中用假时钟控制审批时点。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FakeClock:
    """从固定起点出发，只能向前推进的测试时钟。"""

    def __init__(self, start: datetime):
        if start.tzinfo is None:
            raise ValueError("FakeClock 起点必须带时区")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> datetime:
        from datetime import timedelta

        self._now = self._now + timedelta(**kwargs)
        return self._now

    def set(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("目标时间必须带时区")
        if value < self._now:
            raise ValueError("审计时钟不允许回拨")
        self._now = value
        return self._now
