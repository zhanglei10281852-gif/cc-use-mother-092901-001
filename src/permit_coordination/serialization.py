"""值对象与 JSON 之间的转换。

所有时间统一用 ISO-8601 字符串收发，纳秒以内精度足够审批用途。
反序列化失败统一抛 :class:`ValidationError`，由 HTTP 层返回 400。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .contracts import (
    DriverAuthorization,
    RouteWindow,
    TestCapability,
    VehicleQualification,
)
from .errors import ValidationError


def parse_dt(raw: Any, field_name: str) -> datetime:
    if not isinstance(raw, str):
        raise ValidationError(f"{field_name} 必须是 ISO-8601 字符串")
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"{field_name} 不是合法时间: {raw!r}") from exc
    if value.tzinfo is None:
        raise ValidationError(f"{field_name} 必须携带时区信息")
    return value


def dt_str(value: datetime) -> str:
    return value.isoformat()


def _require_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{key} 必须是非空字符串")
    return value


def vehicle_from_dict(payload: dict[str, Any]) -> VehicleQualification:
    if not isinstance(payload, dict):
        raise ValidationError("车辆资质必须是对象")
    try:
        return VehicleQualification(
            vehicle_id=_require_str(payload, "vehicle_id"),
            plate_number=_require_str(payload, "plate_number"),
            qualification_ref=_require_str(payload, "qualification_ref"),
            qualification_version=_require_str(payload, "qualification_version"),
            valid_from=parse_dt(payload.get("valid_from"), "valid_from"),
            valid_until=parse_dt(payload.get("valid_until"), "valid_until"),
            capabilities=tuple(_require_str_list(payload, "capabilities")),
            issued_by=_require_str(payload, "issued_by"),
        )
    except ValidationError:
        raise
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def driver_from_dict(payload: dict[str, Any]) -> DriverAuthorization:
    if not isinstance(payload, dict):
        raise ValidationError("驾驶员授权必须是对象")
    try:
        return DriverAuthorization(
            driver_id=_require_str(payload, "driver_id"),
            driver_name=_require_str(payload, "driver_name"),
            license_ref=_require_str(payload, "license_ref"),
            valid_from=parse_dt(payload.get("valid_from"), "valid_from"),
            valid_until=parse_dt(payload.get("valid_until"), "valid_until"),
            authorized_capabilities=tuple(
                _require_str_list(payload, "authorized_capabilities")
            ),
            issued_by=_require_str(payload, "issued_by"),
        )
    except ValidationError:
        raise
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def capability_from_dict(payload: dict[str, Any]) -> TestCapability:
    if not isinstance(payload, dict):
        raise ValidationError("测试能力必须是对象")
    try:
        return TestCapability(
            code=_require_str(payload, "code"),
            description=str(payload.get("description", "")),
            cert_ref=_require_str(payload, "cert_ref"),
            valid_until=parse_dt(payload.get("valid_until"), "valid_until"),
        )
    except ValidationError:
        raise
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def window_from_dict(payload: dict[str, Any]) -> RouteWindow:
    if not isinstance(payload, dict):
        raise ValidationError("路线时窗必须是对象")
    try:
        return RouteWindow(
            segment_code=_require_str(payload, "segment_code"),
            starts_at=parse_dt(payload.get("starts_at"), "starts_at"),
            ends_at=parse_dt(payload.get("ends_at"), "ends_at"),
        )
    except ValidationError:
        raise
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def _require_str_list(payload: dict[str, Any], key: str) -> list[str]:
    raw = payload.get(key)
    if not isinstance(raw, list) or not raw:
        raise ValidationError(f"{key} 必须是非空数组")
    result: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValidationError(f"{key} 中每一项都必须是非空字符串")
        result.append(item.strip())
    if len(set(result)) != len(result):
        raise ValidationError(f"{key} 不允许重复")
    return result


def vehicle_to_dict(item: VehicleQualification) -> dict[str, Any]:
    return {
        "vehicle_id": item.vehicle_id,
        "plate_number": item.plate_number,
        "qualification_ref": item.qualification_ref,
        "qualification_version": item.qualification_version,
        "valid_from": dt_str(item.valid_from),
        "valid_until": dt_str(item.valid_until),
        "capabilities": list(item.capabilities),
        "issued_by": item.issued_by,
        "qual_hash": item.qual_hash,
    }


def driver_to_dict(item: DriverAuthorization) -> dict[str, Any]:
    return {
        "driver_id": item.driver_id,
        "driver_name": item.driver_name,
        "license_ref": item.license_ref,
        "valid_from": dt_str(item.valid_from),
        "valid_until": dt_str(item.valid_until),
        "authorized_capabilities": list(item.authorized_capabilities),
        "issued_by": item.issued_by,
        "auth_hash": item.auth_hash,
    }


def capability_to_dict(item: TestCapability) -> dict[str, Any]:
    return {
        "code": item.code,
        "description": item.description,
        "cert_ref": item.cert_ref,
        "valid_until": dt_str(item.valid_until),
    }


def window_to_dict(item: RouteWindow) -> dict[str, Any]:
    return {
        "window_id": item.window_id,
        "segment_code": item.segment_code,
        "starts_at": dt_str(item.starts_at),
        "ends_at": dt_str(item.ends_at),
    }
