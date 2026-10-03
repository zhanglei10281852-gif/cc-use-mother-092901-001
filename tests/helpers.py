"""测试共用工厂：标准申请载荷、角色、假时钟装配。"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from permit_coordination.clock import FakeClock
from permit_coordination.event_store import Actor, EventStore
from permit_coordination.service import PermitService

BASE = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)


def iso(value: datetime) -> str:
    return value.isoformat()


def fleet_actor(name: str = "王队", user_id: str = "U-FLEET-1") -> Actor:
    return Actor(user_id, "fleet_contact", "SH_DEMO", name)


def reviewer_actor(user_id: str = "U-REV-1", name: str = "李审") -> Actor:
    return Actor(user_id, "reviewer", "SH_DEMO", name)


def safety_actor(user_id: str = "U-SAFE", name: str = "安全员赵") -> Actor:
    return Actor(user_id, "safety_officer", "SH_DEMO", name)


def police_actor(user_id: str = "U-POLICE", name: str = "交警钱") -> Actor:
    return Actor(user_id, "traffic_police", "SH_DEMO", name)


def vehicle(
    vehicle_id: str = "V-1",
    qualification_version: str = "v1",
    capabilities=("low_speed", "auto_lane_change"),
    valid_until: datetime | None = None,
    plate_number: str | None = None,
) -> dict:
    return {
        "vehicle_id": vehicle_id,
        "plate_number": plate_number or f"沪A-{vehicle_id}",
        "qualification_ref": f"QUAL-{vehicle_id}",
        "qualification_version": qualification_version,
        "valid_from": iso(BASE - timedelta(days=30)),
        "valid_until": iso(valid_until or BASE + timedelta(days=400)),
        "capabilities": list(capabilities),
        "issued_by": "SH_DEMO",
    }


def driver(
    driver_id: str = "D-1",
    authorized=("low_speed", "auto_lane_change"),
    valid_until: datetime | None = None,
) -> dict:
    return {
        "driver_id": driver_id,
        "driver_name": f"驾驶员-{driver_id}",
        "license_ref": f"LIC-{driver_id}",
        "valid_from": iso(BASE - timedelta(days=30)),
        "valid_until": iso(valid_until or BASE + timedelta(days=400)),
        "authorized_capabilities": list(authorized),
        "issued_by": "SH_DEMO",
    }


def capability(code: str = "low_speed", valid_until: datetime | None = None) -> dict:
    return {
        "code": code,
        "description": f"{code} 能力",
        "cert_ref": f"CERT-{code}",
        "valid_until": iso(valid_until or BASE + timedelta(days=400)),
    }


def window(
    segment: str = "R-S1",
    start: datetime | None = None,
    hours: float = 4.0,
) -> dict:
    start = start or datetime(2026, 10, 21, 1, 0, tzinfo=timezone.utc)
    return {
        "segment_code": segment,
        "starts_at": iso(start),
        "ends_at": iso(start + timedelta(hours=hours)),
    }


def standard_payload(**overrides) -> dict:
    payload = {
        "vehicles": [vehicle()],
        "drivers": [driver()],
        "capabilities": [capability()],
        "route_windows": [window()],
        "note": "智能网联大会联测",
    }
    payload.update(overrides)
    return payload


def build_service(tmp_path: Path | None = None, start: datetime | None = None):
    clock = FakeClock(start or BASE)
    path = str(tmp_path / "events.jsonl") if tmp_path else None
    store = EventStore(path, clock=clock)
    return PermitService(store, clock), store, clock


def create_and_submit(service: PermitService, application_id: str = "AP-1", **payload_over):
    service.create_application(
        application_id,
        fleet_id="FLEET-A",
        jurisdiction="SH_DEMO",
        responsible={"person_id": "P-1", "name": "王队", "contact": "13800000000"},
        actor=fleet_actor(),
    )
    result = service.submit_version(
        application_id, standard_payload(**payload_over), fleet_actor()
    )
    return result


def approve_to_issuance(
    service: PermitService,
    application_id: str = "AP-1",
    *,
    safety: Actor | None = None,
    police: Actor | None = None,
) -> dict:
    """规则校验 + 两会签 + 签发，返回签发结果。"""
    service.record_rule_check(application_id, reviewer_actor())
    service.countersign(application_id, "安全评估通过", safety or safety_actor())
    service.countersign(application_id, "交通组织同意", police or police_actor())
    return service.issue_permit(application_id, reviewer_actor())
