"""道路测试许可协同使用的基础数据契约。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class PermitState(StrEnum):
    DRAFT = "draft"
    REVIEW = "review"
    SIGNED = "signed"
    SUSPENDED = "suspended"
    REVOKED = "revoked"


@dataclass(frozen=True)
class RouteWindow:
    segment_code: str
    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        if self.ends_at <= self.starts_at:
            raise ValueError("路线时窗结束时间必须晚于开始时间")


@dataclass(frozen=True)
class PermitApplication:
    application_id: str
    version: int
    vehicle_ids: tuple[str, ...]
    driver_ids: tuple[str, ...]
    route_windows: tuple[RouteWindow, ...]
    state: PermitState = PermitState.DRAFT

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("申请版本必须从一开始")
        if not self.vehicle_ids or not self.driver_ids or not self.route_windows:
            raise ValueError("申请必须包含车辆、驾驶员和路线时窗")
