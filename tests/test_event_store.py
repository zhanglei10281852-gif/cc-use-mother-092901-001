"""事件日志持久化：重载、序号完整性、乐观并发。"""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from helpers import build_service, create_and_submit, fleet_actor, reviewer_actor
from permit_coordination.clock import FakeClock
from permit_coordination.event_store import Actor, EventStore
from permit_coordination.errors import ConflictError, ValidationError

START = datetime(2026, 10, 20, tzinfo=timezone.utc)


class EventStorePersistenceTests(unittest.TestCase):
    def test_state_rebuilds_after_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            clock = FakeClock(START)
            store = EventStore(path, clock=clock)
            from permit_coordination.service import PermitService

            service = PermitService(store, clock)
            create_and_submit(service, "AP-1")

            # 用新的存储实例重新打开同一文件
            store2 = EventStore(path, clock=clock)
            service2 = PermitService(store2, clock)
            view = service2.get_application_view("AP-1")
            self.assertEqual(view["current_version"], 1)
            self.assertEqual(view["jurisdiction"], "SH_DEMO")
            self.assertEqual(view["responsible"]["person_id"], "P-1")

    def test_optimistic_concurrency_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EventStore(Path(tmp) / "e.jsonl", clock=FakeClock(START))
            actor = Actor("u", "reviewer", "SH_DEMO")
            store.append("app:X", "ApplicationCreated", {"x": 1}, actor, expected_version=0)
            with self.assertRaises(ConflictError):
                store.append(
                    "app:X", "ApplicationCreated", {"x": 2}, actor, expected_version=0
                )

    def test_gap_in_sequence_is_detected_as_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "e.jsonl"
            store = EventStore(path, clock=FakeClock(START))
            actor = Actor("u", "reviewer", "SH_DEMO")
            store.append("app:X", "ApplicationCreated", {"x": 1}, actor, expected_version=0)
            # 手工追加一条序号错误的记录
            good = path.read_text(encoding="utf-8").strip().splitlines()[0]
            import json

            bad_event = json.loads(good)
            bad_event["seq"] = 99
            bad_event["stream_id"] = "app:Y"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(bad_event, ensure_ascii=False) + "\n")
            with self.assertRaises(ValidationError):
                EventStore(path, clock=FakeClock(START))

    def test_actor_identity_is_persisted_on_every_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "e.jsonl"
            clock = FakeClock(START)
            from permit_coordination.service import PermitService

            service = PermitService(EventStore(path, clock=clock), clock)
            create_and_submit(service, "AP-1")
            service.record_rule_check("AP-1", reviewer_actor())
            timeline = service.timeline("AP-1")["events"]
            submit = next(e for e in timeline if e["type"] == "VersionSubmitted")
            check = next(e for e in timeline if e["type"] == "RuleCheckRecorded")
            self.assertEqual(submit["actor"]["role"], "fleet_contact")
            self.assertEqual(check["actor"]["user_id"], "U-REV-1")
            self.assertEqual(check["actor"]["jurisdiction"], "SH_DEMO")


if __name__ == "__main__":
    unittest.main()
