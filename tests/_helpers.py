"""测试夹具：标准合格申请载荷与常用流程。"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from permit_coordination.clock import FakeClock
from permit_coordination.service import PermitService
from permit_coordination.store import InMemoryEventStore

BASE = datetime(2026, 10, 21, 8, 0, tzinfo=timezone.utc)
TZ = "+00:00"


def iso(dt: datetime) -> str:
    return dt.isoformat()


def cover(start: datetime | None = None, end: datetime | None = None) -> dict:
    return {
        "valid_from": iso(start or BASE - timedelta(days=10)),
        "valid_to": iso(end or BASE + timedelta(days=30)),
    }


def window(segment: str, start: datetime, hours: int = 2) -> dict:
    return {"segment_code": segment, "starts_at": iso(start),
            "ends_at": iso(start + timedelta(hours=hours))}


def valid_payload(**overrides) -> dict:
    payload = {
        "application_id": "AP-1",
        "fleet_id": "FLEET-A",
        "jurisdiction": "JINGHAI",
        "vehicles": {
            "V-1": {"plate": "沪D10001",
                    "qualifications": [{"type": "road_test_qualification", **cover()}],
                    "insurance": {"policy_no": "INS-1", **cover()}},
        },
        "drivers": {
            "D-1": {"name": "张三",
                    "authorizations": [
                        {"type": "safety_driver", **cover()},
                        {"type": "automated_driving_training", **cover()}]},
        },
        "capability": {
            "level": "L4",
            "functions": ["emergency_stop", "remote_monitoring", "data_recording"],
            "certifications": ["technical_guidelines_compliance"],
        },
        "route_windows": [window("R-JH-01", BASE)],
    }
    payload.update(overrides)
    return payload


def build_service(at: datetime | None = None) -> tuple[PermitService, FakeClock]:
    clock = FakeClock(at or BASE - timedelta(days=7))
    service = PermitService(InMemoryEventStore(), clock=clock)
    return service, clock


def approve_jurisdictions(service, application_id, version, jurisdictions,
                          actor_suffix="reviewer"):
    ids = []
    for jurisdiction in jurisdictions:
        opened = service.open_review(application_id, version, jurisdiction,
                                     actor=f"{jurisdiction.lower()}-{actor_suffix}")
        review_id = opened["review_id"]
        if opened.get("findings"):
            codes = sorted({f["code"] for f in opened["findings"]})
            service.submit_supplement(
                review_id,
                [{"document_id": f"DOC-{code}", "title": code} for code in codes],
                codes, actor="fleet-clerk")
        service.approve_review(review_id, actor=f"{jurisdiction.lower()}-{actor_suffix}")
        ids.append(review_id)
    return ids


def issue_for(service, payload=None, application_id="AP-1", version=1,
              jurisdictions=("JINGHAI",), actor="jh-director"):
    if payload is not None:
        service.submit_application(payload, actor="fleet-clerk")
    approve_jurisdictions(service, application_id, version, jurisdictions)
    return service.issue_permit(application_id, version, actor=actor)
