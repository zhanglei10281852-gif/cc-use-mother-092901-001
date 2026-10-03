"""HTTP/JSON 端到端测试：真实起服务、真实发请求。"""

import json
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from helpers import standard_payload
from permit_coordination.clock import FakeClock
from permit_coordination.event_store import EventStore
from permit_coordination.http_api import ApiContainer, build_server

BASE = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)


class HttpClient:
    def __init__(self, base_url: str):
        self.base_url = base_url

    def request(self, method: str, path: str, body=None, actor=None):
        url = self.base_url + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if actor:
            req.add_header("X-User-Id", actor["user_id"])
            req.add_header("X-Role", actor["role"])
            req.add_header("X-Jurisdiction", actor.get("jurisdiction", ""))
            req.add_header(
                "X-User-Name", urllib.parse.quote(actor.get("name", ""))
            )
        try:
            with urllib.request.urlopen(req) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                return resp.status, payload
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            return exc.code, payload


FLEET = {"user_id": "U-F", "role": "fleet_contact", "jurisdiction": "SH_DEMO", "name": "王队"}
REVIEWER = {"user_id": "U-R", "role": "reviewer", "jurisdiction": "SH_DEMO", "name": "李审"}
SAFETY = {"user_id": "U-S", "role": "safety_officer", "jurisdiction": "SH_DEMO", "name": "赵安"}
POLICE = {"user_id": "U-P", "role": "traffic_police", "jurisdiction": "SH_DEMO", "name": "钱警"}


def win(segment: str, day: int, hour: int = 1):
    return {
        "segment_code": segment,
        "starts_at": f"2026-10-{day:02d}T0{hour}:00:00+00:00",
        "ends_at": f"2026-10-{day:02d}T0{hour + 4}:00:00+00:00",
    }


class HttpEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clock = FakeClock(BASE)
        cls.store = EventStore(None, clock=cls.clock)
        container = ApiContainer(cls.store, clock=cls.clock)
        cls.server = build_server("127.0.0.1", 0, container)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.api = HttpClient(f"http://127.0.0.1:{cls.port}")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _create_app(self, app_id="AP-H1"):
        status, body = self.api.request(
            "POST",
            "/api/applications",
            {
                "application_id": app_id,
                "fleet_id": "FLEET-WEB",
                "jurisdiction": "SH_DEMO",
                "responsible": {"person_id": "P-9", "name": "王队", "contact": "139"},
            },
            FLEET,
        )
        self.assertEqual(status, 200, body)
        return body

    def _submit(self, app_id="AP-H1", payload=None):
        return self.api.request(
            "POST",
            f"/api/applications/{app_id}/versions",
            payload or standard_payload(),
            FLEET,
        )

    def _approve(self, app_id="AP-H1"):
        self.assertEqual(self.api.request("POST", f"/api/applications/{app_id}/rule-checks", {}, REVIEWER)[0], 200)
        self.assertEqual(self.api.request("POST", f"/api/applications/{app_id}/countersignatures", {"comment": "安全"}, SAFETY)[0], 200)
        self.assertEqual(self.api.request("POST", f"/api/applications/{app_id}/countersignatures", {"comment": "交警"}, POLICE)[0], 200)
        status, body = self.api.request("POST", f"/api/applications/{app_id}/issue", {}, REVIEWER)
        self.assertEqual(status, 200, body)
        return body

    def test_health(self):
        status, body = self.api.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_full_permit_lifecycle_over_http(self):
        self._create_app()
        payload = standard_payload(route_windows=[win("R-S3", 26)])
        status, submitted = self._submit("AP-H1", payload)
        self.assertEqual(status, 200)
        self.assertEqual(submitted["version"], 1)

        # 重复内容提交 -> 409
        status, err = self._submit("AP-H1", payload)
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "conflict")

        issued = self._approve()
        self.assertTrue(issued["permit_number"])

        status, effective = self.api.request("GET", "/api/applications/AP-H1/effective")
        self.assertEqual(status, 200)
        self.assertEqual(effective["state"], "signed")
        self.assertEqual(effective["responsible"]["name"], "王队")
        self.assertGreaterEqual(len(effective["active_grants"]), 4)

    def test_missing_actor_headers_returns_400(self):
        status, err = self.api.request("POST", "/api/applications/AP-H2/versions", standard_payload())
        self.assertEqual(status, 400)
        self.assertEqual(err["error"], "validation_error")

    def test_occupancy_conflict_returns_409(self):
        self._create_app("AP-W1")
        self._submit("AP-W1", standard_payload(route_windows=[win("R-S1", 21)]))
        self._approve("AP-W1")

        self._create_app("AP-W2")
        status, body = self._submit("AP-W2", standard_payload(route_windows=[win("R-S1", 21, 2)]))
        self.assertEqual(status, 200)
        self.api.request("POST", "/api/applications/AP-W2/rule-checks", {}, REVIEWER)
        self.api.request("POST", "/api/applications/AP-W2/countersignatures", {"comment": "s"}, SAFETY)
        self.api.request("POST", "/api/applications/AP-W2/countersignatures", {"comment": "p"}, POLICE)
        status, err = self.api.request("POST", "/api/applications/AP-W2/issue", {}, REVIEWER)
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "occupancy_conflict")
        self.assertEqual(err["details"]["conflicts"][0]["other_application_id"], "AP-W1")

    def test_closure_resolution_trace_over_http(self):
        self._create_app("AP-W3")
        self._submit("AP-W3", standard_payload(route_windows=[win("R-S2", 23)]))
        self._approve("AP-W3")
        self.clock.set(datetime(2026, 10, 23, 1, 30, tzinfo=timezone.utc))

        status, closure_body = self.api.request(
            "POST",
            "/api/closures",
            {
                "closure_id": "CL-WEB-1",
                "segment_code": "R-S2",
                "starts_at": "2026-10-23T02:00:00+00:00",
                "ends_at": "2026-10-23T04:00:00+00:00",
                "reason": "临时交通管制",
            },
            REVIEWER,
        )
        self.assertEqual(status, 200, closure_body)
        self.assertEqual(closure_body["conflict_count"], 1)
        cid = closure_body["conflict_ids"][0]

        self.assertEqual(self.api.request("POST", f"/api/conflicts/{cid}/proposal", {}, REVIEWER)[0], 200)
        self.assertEqual(
            self.api.request("POST", f"/api/conflicts/{cid}/response", {"accepted": True, "comment": "同意"}, FLEET)[0],
            200,
        )
        self.assertEqual(
            self.api.request("POST", f"/api/conflicts/{cid}/countersign", {"comment": "确认"}, POLICE)[0],
            200,
        )
        status, closed = self.api.request("POST", f"/api/conflicts/{cid}/close", {}, REVIEWER)
        self.assertEqual(status, 200, closed)
        self.assertEqual(closed["outcome"], "rescheduled")

        status, conflict = self.api.request("GET", f"/api/conflicts/{cid}")
        self.assertEqual(status, 200)
        self.assertEqual(conflict["status"], "resolved_rescheduled")
        self.assertTrue(conflict["closed"]["permit_amendment_event_id"])

    def test_as_of_query_restores_historical_state(self):
        self._create_app("AP-W4")
        self._submit("AP-W4", standard_payload(route_windows=[win("R-S1", 24)]))
        before_issue = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
        self.clock.set(before_issue)
        self._approve("AP-W4")
        status, historical = self.api.request(
            "GET",
            "/api/applications/AP-W4/effective?at="
            + urllib.parse.quote(datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc).isoformat()),
        )
        self.assertEqual(status, 200)
        self.assertIsNone(historical["permit_number"])
        status, current = self.api.request("GET", "/api/applications/AP-W4/effective")
        self.assertEqual(current["state"], "signed")

    def test_recognition_flow_over_http(self):
        self._create_app("AP-W5")
        self._submit("AP-W5", standard_payload(route_windows=[win("R-S3", 25)]))
        self._approve("AP-W5")
        sz = {"user_id": "U-SZ", "role": "reviewer", "jurisdiction": "SZ_DEMO", "name": "深审"}
        status, body = self.api.request(
            "POST",
            "/api/recognitions",
            {"source_application_id": "AP-W5", "recognizing_jurisdiction": "SZ_DEMO"},
            sz,
        )
        self.assertEqual(status, 200, body)
        rec_id = body["recognition_id"]
        status, rec = self.api.request("GET", f"/api/recognitions/{rec_id}")
        self.assertEqual(status, 200)
        self.assertEqual(rec["recognizing_jurisdiction"], "SZ_DEMO")
        self.assertEqual(status, 200)

    def test_audit_events_endpoint_lists_immutable_facts(self):
        self._create_app("AP-W6")
        self._submit("AP-W6")
        status, body = self.api.request("GET", "/api/audit/events")
        self.assertEqual(status, 200)
        types = [e["type"] for e in body["events"]]
        self.assertIn("ApplicationCreated", types)
        self.assertIn("VersionSubmitted", types)
        # 全局序号连续
        seqs = [e["seq"] for e in body["events"]]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))


if __name__ == "__main__":
    unittest.main()
