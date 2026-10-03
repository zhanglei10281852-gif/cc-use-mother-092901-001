"""道路测试许可协同使用的基础数据契约。

本模块只描述稳定的值对象与状态枚举，不依赖存储或 Web 框架。
所有时间必须是带时区信息的 ``datetime``，保证跨辖区比较时没有歧义。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class PermitState(StrEnum):
    """申请/许可生命周期状态。"""

    DRAFT = "draft"
    REVIEW = "review"
    INFO_REQUESTED = "info_requested"
    SIGNED = "signed"
    SUSPENDED = "suspended"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"


class ApprovalKind(StrEnum):
    VEHICLE = "vehicle"
    DRIVER = "driver"
    CAPABILITY = "capability"
    ROUTE_WINDOW = "route_window"


class ApprovalStatus(StrEnum):
    ACTIVE = "active"
    INVALIDATED = "invalidated"


class ConflictStatus(StrEnum):
    OPEN = "open"
    RESCHEDULE_PROPOSED = "reschedule_proposed"
    REJECTED_PENDING_REVIEW = "rejected_pending_review"
    RESOLVED_RESCHEDULED = "resolved_rescheduled"
    RESOLVED_CANCELLED = "resolved_cancelled"


class RecognitionState(StrEnum):
    RECOGNIZED = "recognized"
    WITHDRAWN = "withdrawn"


def require_aware(value: datetime, field_name: str) -> datetime:
    """拒绝无时区时间，避免不同辖区对同一时刻产生不同解释。"""
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} 必须是 ISO 时间戳")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} 必须携带时区信息")
    return value


def content_hash(payload: Any) -> str:
    """对任意可 JSON 化内容计算稳定的短哈希，用于版本去重与变更比对。"""
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class RouteWindow:
    segment_code: str
    starts_at: datetime
    ends_at: datetime
    window_id: str = field(default="")

    def __post_init__(self) -> None:
        if not self.segment_code:
            raise ValueError("路段编号不能为空")
        require_aware(self.starts_at, "starts_at")
        require_aware(self.ends_at, "ends_at")
        if self.ends_at <= self.starts_at:
            raise ValueError("路线时窗结束时间必须晚于开始时间")
        if not self.window_id:
            object.__setattr__(
                self,
                "window_id",
                "w_"
                + content_hash(
                    [
                        self.segment_code,
                        self.starts_at.isoformat(),
                        self.ends_at.isoformat(),
                    ]
                ),
            )

    def duration_hours(self) -> float:
        return (self.ends_at - self.starts_at).total_seconds() / 3600.0

    def overlaps(self, other: "RouteWindow") -> bool:
        """同一路段上的半开区间是否重叠。"""
        if self.segment_code != other.segment_code:
            return False
        return self.starts_at < other.ends_at and other.starts_at < self.ends_at


@dataclass(frozen=True)
class VehicleQualification:
    vehicle_id: str
    plate_number: str
    qualification_ref: str
    qualification_version: str
    valid_from: datetime
    valid_until: datetime
    capabilities: tuple[str, ...]
    issued_by: str
    qual_hash: str = field(default="")

    def __post_init__(self) -> None:
        if not self.vehicle_id:
            raise ValueError("车辆编号不能为空")
        require_aware(self.valid_from, "valid_from")
        require_aware(self.valid_until, "valid_until")
        if self.valid_until <= self.valid_from:
            raise ValueError("车辆资质有效期结束必须晚于开始")
        if not self.capabilities:
            raise ValueError("车辆资质至少声明一项测试能力")
        if not self.qual_hash:
            object.__setattr__(self, "qual_hash", content_hash(self._hashable()))

    def _hashable(self) -> list[Any]:
        return [
            self.plate_number,
            self.qualification_ref,
            self.qualification_version,
            self.valid_from.isoformat(),
            self.valid_until.isoformat(),
            sorted(self.capabilities),
            self.issued_by,
        ]


@dataclass(frozen=True)
class DriverAuthorization:
    driver_id: str
    driver_name: str
    license_ref: str
    valid_from: datetime
    valid_until: datetime
    authorized_capabilities: tuple[str, ...]
    issued_by: str
    auth_hash: str = field(default="")

    def __post_init__(self) -> None:
        if not self.driver_id:
            raise ValueError("驾驶员编号不能为空")
        require_aware(self.valid_from, "valid_from")
        require_aware(self.valid_until, "valid_until")
        if self.valid_until <= self.valid_from:
            raise ValueError("驾驶员授权有效期结束必须晚于开始")
        if not self.authorized_capabilities:
            raise ValueError("驾驶员授权至少包含一项能力")
        if not self.auth_hash:
            object.__setattr__(self, "auth_hash", content_hash(self._hashable()))

    def _hashable(self) -> list[Any]:
        return [
            self.driver_name,
            self.license_ref,
            self.valid_from.isoformat(),
            self.valid_until.isoformat(),
            sorted(self.authorized_capabilities),
            self.issued_by,
        ]


@dataclass(frozen=True)
class TestCapability:
    code: str
    description: str
    cert_ref: str
    valid_until: datetime

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("测试能力编码不能为空")
        require_aware(self.valid_until, "valid_until")


@dataclass(frozen=True)
class ResponsiblePerson:
    """申请方责任人，车队现场联系与追责的落点。"""

    person_id: str
    name: str
    contact: str = ""


@dataclass(frozen=True)
class ApplicationVersion:
    """一次提交形成的不可变版本快照。"""

    version: int
    vehicles: tuple[VehicleQualification, ...]
    drivers: tuple[DriverAuthorization, ...]
    capabilities: tuple[TestCapability, ...]
    route_windows: tuple[RouteWindow, ...]
    note: str
    content_digest: str
    submitted_at: datetime


@dataclass(frozen=True)
class PermitApplication:
    """申请的轻量摘要契约（保持早期接口兼容）。"""

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
