"""可注入的时钟，生产用系统时钟，测试用可控时钟。"""

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FakeClock:
    """测试时钟：显式设置当前时间，事件时间因此可重放。"""

    def __init__(self, at: datetime | None = None) -> None:
        self._at = at or datetime(2026, 10, 1, tzinfo=timezone.utc)

    def set(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("时间必须携带时区信息")
        self._at = at

    def advance(self, **kwargs) -> datetime:
        from datetime import timedelta

        self._at = self._at + timedelta(**kwargs)
        return self._at

    def now(self) -> datetime:
        return self._at
