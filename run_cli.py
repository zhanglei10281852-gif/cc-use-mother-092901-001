"""端到端冒烟脚本：在内存中走通 提交→审查→补件→会签→签发→互认→封路改期→追溯。"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from permit_coordination.clock import FakeClock
from permit_coordination.service import PermitService
from permit_coordination.store import InMemoryEventStore

BASE = datetime(2026, 10, 21, 8, 0, tzinfo=timezone.utc)
clock = FakeClock(BASE - timedelta(days=7))
service = PermitService(InMemoryEventStore(), clock=clock)


def cover(days=30):
    return {
        "valid_from": (BASE - timedelta(days=10)).isoformat(),
        "valid_to": (BASE + timedelta(days=days)).isoformat(),
    }


payload = {
    "application_id": "AP-DEMO",
    "fleet_id": "FLEET-A",
    "jurisdiction": "JINGHAI",
    "idempotency_key": "demo-key-1",
    "vehicles": {
        "V-1": {"plate": "沪D12345",
                "qualifications": [{"type": "road_test_qualification", **cover()}],
                "insurance": {"policy_no": "INS-1", **cover()}},
    },
    "drivers": {
        "D-1": {"name": "张三",
                "authorizations": [
                    {"type": "safety_driver", **cover()},
                    {"type": "automated_driving_training", **cover()}]},
    },
    "capability": {"level": "L4",
                   "functions": ["emergency_stop", "remote_monitoring", "data_recording"],
                   "certifications": ["technical_guidelines_compliance"]},
    "route_windows": [
        {"segment_code": "R-JH-01", "starts_at": BASE.isoformat(),
         "ends_at": (BASE + timedelta(hours=2)).isoformat()},
    ],
}

first = service.submit_application(payload, actor="fleet-A-clerk")
duplicate = service.submit_application(payload, actor="fleet-A-clerk")  # 同内容重复提交

review = service.open_review("AP-DEMO", 1, "JINGHAI", actor="jh-reviewer")
# 演示中规则全过；若有补件项，申请方补件后再批准
service.approve_review(review["review_id"], conditions=["全程开启数据回传"], actor="jh-reviewer")

clock.set(BASE - timedelta(days=6, hours=12))
issued = service.issue_permit("AP-DEMO", 1, actor="jh-director")

service.record_recognition(
    issued["permit_id"], "JIADING",
    {"segment_codes": ["R-JH-01"], "vehicle_ids": ["V-1"]},
    basis="长三角示范区互认协议第3条", actor="jd-observer")

clock.set(BASE - timedelta(hours=2))
closure = service.register_road_closure(
    ["R-JH-01"], BASE.isoformat(), (BASE + timedelta(hours=1)).isoformat(),
    reason="重大活动安保", actor="traffic-police")
service.resolve_conflict(
    closure["conflict_ids"][0], "rescheduled", actor="fleet-A-clerk",
    new_window={"segment_code": "R-JH-01",
                "starts_at": (BASE + timedelta(hours=3)).isoformat(),
                "ends_at": (BASE + timedelta(hours=5)).isoformat()},
    note="避开封路时段")

clock.set(BASE + timedelta(hours=4))
snapshot = service.snapshot_at(clock.now())

print(json.dumps({
    "submission": first,
    "duplicate_submission": duplicate,
    "issued": issued,
    "closure": closure,
    "snapshot_as_of_test_time": {
        "as_of": snapshot["as_of"],
        "permit_ids": [p["permit_id"] for p in snapshot["effective_permits"]],
        "windows": snapshot["effective_permits"][0]["route_windows"] if snapshot["effective_permits"] else [],
    },
}, ensure_ascii=False, indent=2, default=list))
