"""JSON Lines 事件存储持久化与重放测试。"""

import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from permit_coordination.clock import FakeClock
from permit_coordination.service import PermitService
from permit_coordination.store import JsonFileEventStore
from tests._helpers import BASE, approve_jurisdictions, build_service, iso, valid_payload


class PersistenceTests(unittest.TestCase):
    def test_state_reconstructs_from_jsonl_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            clock = FakeClock(BASE - timedelta(days=7))

            store = JsonFileEventStore(path)
            service = PermitService(store, clock=clock)
            service.submit_application(valid_payload(), actor="fleet-clerk")
            approve_jurisdictions(service, "AP-1", 1, ("JINGHAI",))
            issued = service.issue_permit("AP-1", 1, actor="jh-director")
            permit_id = issued["permit_id"]
            service.suspend_permit(permit_id, "天气管制", actor="jh-director")

            # 每行都是合法 JSON 事件
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertGreaterEqual(len(lines), 5)
            for line in lines:
                self.assertIn("type", json.loads(line))

            # 重启：新存储 + 新服务，状态完整还原
            store2 = JsonFileEventStore(path)
            service2 = PermitService(store2, clock=clock)
            detail = service2.permit_detail(permit_id)
            self.assertEqual(detail["status"], "suspended")
            changes = [(c["to"], c["actor"]) for c in detail["status_changes"]]
            self.assertEqual(changes, [("issued", "jh-director"),
                                       ("suspended", "jh-director")])
            self.assertEqual(detail["review_basis"][0]["decided_by"], "jinghai-reviewer")

            # 重启后幂等映射同样恢复：同键重复提交不产生新事件
            before = len(lines)
            result = service2.submit_application(
                valid_payload(idempotency_key="persisted-key"), actor="fleet-clerk")
            service2.submit_application(
                valid_payload(idempotency_key="persisted-key"), actor="fleet-clerk")
            self.assertTrue(result)
            store3 = JsonFileEventStore(path)
            service3 = PermitService(store3, clock=clock)
            self.assertEqual(len(service3.event_journal()), before + 1)

    def test_replay_upto_matches_original_snapshot(self):
        service, clock = build_service()
        service.submit_application(valid_payload(), actor="fleet-clerk")
        approve_jurisdictions(service, "AP-1", 1, ("JINGHAI",))
        service.issue_permit("AP-1", 1, actor="jh-director")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            for event in service.store.all():
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
            restarted = PermitService(JsonFileEventStore(path), clock=clock)
            at = BASE + timedelta(minutes=10)
            snap = restarted.snapshot_at(at)
            self.assertEqual(len(snap["effective_permits"]), 1)
            self.assertEqual(snap["effective_permits"][0]["issued_by"], "jh-director")


if __name__ == "__main__":
    unittest.main()
