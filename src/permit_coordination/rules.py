"""辖区审查规则。

每个辖区一份规则集（带 ``rules_version``，规则版本随审批依据留痕），
对申请版本快照做静态校验，输出分级 findings。
severity 为 blocker 的问题未消除（或被带理由豁免）时不允许签发。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .contracts import ApplicationVersion


@dataclass(frozen=True)
class JurisdictionRules:
    jurisdiction: str
    rules_version: str
    max_window_hours: float
    require_capabilities: tuple[str, ...]
    allowed_segments: tuple[str, ...] | None  # None 表示不做路段白名单限制
    min_qual_validity_hours: float

    def evaluate(
        self,
        snapshot: ApplicationVersion,
        at: datetime,
    ) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []

        # 0. 辖区要求必须申报的测试能力
        capability_codes = {cap.code for cap in snapshot.capabilities}
        for required in self.require_capabilities:
            if required not in capability_codes:
                findings.append(_blocker(
                    "required_capability_missing",
                    f"{self.jurisdiction} 要求申报能力 {required}，申请中未包含",
                    required,
                    {"required": required},
                ))

        # 1. 车辆资质：在审查时点必须在有效期内，并留出最低余量。
        for vehicle in snapshot.vehicles:
            if vehicle.valid_from > at:
                findings.append(_blocker(
                    "vehicle_not_yet_valid",
                    f"车辆 {vehicle.vehicle_id} 资质 {vehicle.qualification_ref} 尚未生效",
                    vehicle.vehicle_id,
                    {"valid_from": vehicle.valid_from.isoformat()},
                ))
            if vehicle.valid_until <= at:
                findings.append(_blocker(
                    "vehicle_expired",
                    f"车辆 {vehicle.vehicle_id} 资质已过期",
                    vehicle.vehicle_id,
                    {"valid_until": vehicle.valid_until.isoformat()},
                ))

        # 2. 驾驶员授权：有效期 + 是否覆盖申请的测试能力。
        for driver in snapshot.drivers:
            if driver.valid_until <= at:
                findings.append(_blocker(
                    "driver_authorization_expired",
                    f"驾驶员 {driver.driver_id} 授权已过期",
                    driver.driver_id,
                    {"valid_until": driver.valid_until.isoformat()},
                ))
            missing = capability_codes - set(driver.authorized_capabilities)
            if missing:
                findings.append(_blocker(
                    "driver_missing_capability",
                    f"驾驶员 {driver.driver_id} 未覆盖能力: {sorted(missing)}",
                    driver.driver_id,
                    {"missing": sorted(missing)},
                ))

        # 3. 车辆能力同样要覆盖申报能力。
        for vehicle in snapshot.vehicles:
            missing = capability_codes - set(vehicle.capabilities)
            if missing:
                findings.append(_blocker(
                    "vehicle_missing_capability",
                    f"车辆 {vehicle.vehicle_id} 不具备能力: {sorted(missing)}",
                    vehicle.vehicle_id,
                    {"missing": sorted(missing)},
                ))

        # 4. 能力证书有效期。
        for cap in snapshot.capabilities:
            if cap.valid_until <= at:
                findings.append(_blocker(
                    "capability_cert_expired",
                    f"测试能力 {cap.code} 证书 {cap.cert_ref} 已过期",
                    cap.code,
                    {"valid_until": cap.valid_until.isoformat()},
                ))

        # 5. 时窗长度、所属路段、时窗是否已在过去。
        for window in snapshot.route_windows:
            if window.duration_hours() > self.max_window_hours:
                findings.append(_blocker(
                    "window_too_long",
                    f"路段 {window.segment_code} 时窗 {window.duration_hours():.1f}h "
                    f"超过上限 {self.max_window_hours:g}h",
                    window.window_id,
                    {
                        "segment": window.segment_code,
                        "hours": window.duration_hours(),
                        "limit": self.max_window_hours,
                    },
                ))
            if self.allowed_segments is not None and window.segment_code not in self.allowed_segments:
                findings.append(_blocker(
                    "segment_not_allowed",
                    f"路段 {window.segment_code} 不在 {self.jurisdiction} 开放清单内",
                    window.window_id,
                    {"segment": window.segment_code},
                ))
            if window.ends_at <= at:
                findings.append(_warning(
                    "window_already_passed",
                    f"路段 {window.segment_code} 时窗已结束，无需占用",
                    window.window_id,
                ))

        # 6. 最低资质余量（临期资质提示审查员关注，warning 不阻断签发）。
        horizon_seconds = self.min_qual_validity_hours * 3600
        for vehicle in snapshot.vehicles:
            remaining = (vehicle.valid_until - at).total_seconds()
            if 0 < remaining < horizon_seconds:
                findings.append(_warning(
                    "vehicle_qualification_expiring",
                    f"车辆 {vehicle.vehicle_id} 资质将在 {self.min_qual_validity_hours:g}h 内到期",
                    vehicle.vehicle_id,
                ))
        for driver in snapshot.drivers:
            remaining = (driver.valid_until - at).total_seconds()
            if 0 < remaining < horizon_seconds:
                findings.append(_warning(
                    "driver_authorization_expiring",
                    f"驾驶员 {driver.driver_id} 授权将在 {self.min_qual_validity_hours:g}h 内到期",
                    driver.driver_id,
                ))

        return findings

    def blockers(self, snapshot: ApplicationVersion, at: datetime) -> list[dict[str, Any]]:
        return [f for f in self.evaluate(snapshot, at) if f["severity"] == "blocker"]


def _blocker(code: str, message: str, subject: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"severity": "blocker", "code": code, "message": message, "subject": subject, **(extra or {})}


def _warning(code: str, message: str, subject: str) -> dict[str, Any]:
    return {"severity": "warning", "code": code, "message": message, "subject": subject}


DEFAULT_RULES: dict[str, JurisdictionRules] = {
    "SH_DEMO": JurisdictionRules(
        jurisdiction="SH_DEMO",
        rules_version="2026.09",
        max_window_hours=8.0,
        require_capabilities=("low_speed",),
        allowed_segments=("R-S1", "R-S2", "R-S3", "R-X1"),
        min_qual_validity_hours=72.0,
    ),
    "SZ_DEMO": JurisdictionRules(
        jurisdiction="SZ_DEMO",
        rules_version="2026.10",
        max_window_hours=6.0,
        require_capabilities=("low_speed",),
        allowed_segments=("R-Z1", "R-Z2", "R-X1"),
        min_qual_validity_hours=48.0,
    ),
}


def get_rules(jurisdiction: str) -> JurisdictionRules:
    try:
        return DEFAULT_RULES[jurisdiction]
    except KeyError:
        raise KeyError(f"辖区 {jurisdiction} 未配置审查规则")
