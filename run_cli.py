"""零依赖冒烟脚本：内存事件库上走完一次申请 → 签发。

生产用法见 README：python -m permit_coordination --port 8080
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from permit_coordination.clock import FakeClock
from permit_coordination.event_store import Actor, EventStore
from permit_coordination.service import PermitService

START = datetime(2026, 10, 20, tzinfo=timezone.utc)


def main() -> None:
    clock = FakeClock(START)
    service = PermitService(EventStore(None, clock=clock), clock)

    fleet = Actor("U-FLEET-01", "fleet_contact", "SH_DEMO", "王队")
    reviewer = Actor("U-REV-01", "reviewer", "SH_DEMO", "李审")
    safety = Actor("U-SAFE-01", "safety_officer", "SH_DEMO", "安全员赵")
    police = Actor("U-POL-01", "traffic_police", "SH_DEMO", "交警钱")

    service.create_application(
        "AP-SMOKE",
        fleet_id="FLEET-DEMO",
        jurisdiction="SH_DEMO",
        responsible={"person_id": "P-1", "name": "王队", "contact": "13800000000"},
        actor=fleet,
    )
    window = {
        "segment_code": "R-S1",
        "starts_at": (START + timedelta(days=1, hours=1)).isoformat(),
        "ends_at": (START + timedelta(days=1, hours=5)).isoformat(),
    }
    vehicle = {
        "vehicle_id": "VIN-01",
        "plate_number": "沪A0001试",
        "qualification_ref": "QUAL-01",
        "qualification_version": "2026.1",
        "valid_from": (START - timedelta(days=30)).isoformat(),
        "valid_until": (START + timedelta(days=400)).isoformat(),
        "capabilities": ["low_speed"],
        "issued_by": "SH_DEMO",
    }
    driver = {
        "driver_id": "DRV-01",
        "driver_name": "张三",
        "license_ref": "LIC-01",
        "valid_from": (START - timedelta(days=30)).isoformat(),
        "valid_until": (START + timedelta(days=400)).isoformat(),
        "authorized_capabilities": ["low_speed"],
        "issued_by": "SH_DEMO",
    }
    capability = {
        "code": "low_speed",
        "description": "低速跟车",
        "cert_ref": "CERT-LS",
        "valid_until": (START + timedelta(days=400)).isoformat(),
    }
    service.submit_version(
        "AP-SMOKE",
        {
            "vehicles": [vehicle],
            "drivers": [driver],
            "capabilities": [capability],
            "route_windows": [window],
            "note": "冒烟",
        },
        fleet,
    )
    check = service.record_rule_check("AP-SMOKE", reviewer)
    service.countersign("AP-SMOKE", "安全评估通过", safety)
    service.countersign("AP-SMOKE", "交通组织同意", police)
    issued = service.issue_permit("AP-SMOKE", reviewer)

    print(
        json.dumps(
            {
                "permit_number": issued["permit_number"],
                "blocker_count": check["blocker_count"],
                "new_grant_count": issued["new_grant_count"],
                "state": service.get_application_view("AP-SMOKE")["state"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
