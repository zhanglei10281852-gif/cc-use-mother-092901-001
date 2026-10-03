"""HTTP/JSON 端到端测试：真实端口上的完整协作流程。"""

import json
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from http.client import RemoteDisconnected
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from permit_coordination.clock import FakeClock
from permit_coordination.httpapi import build_server
from permit_coordination.store import InMemoryEventStore
from tests._helpers import BASE, iso, valid_payload, window
if sys.version_info >= (3, 10):
    pass


class HttpClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def request(self, method: str, path: str, body=None, actor=None, headers=None):
        url = self.base_url + path
        data = None
        hdrs = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json; charset=utf-8"
        if actor:
            hdrs["X-Actor"] = actor
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, json.loads(raw) if raw else {}


class HttpEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock(BASE - timedelta(days=7))
        self.httpd = build_server("127.0.0.1", 0, store=InMemoryEventStore(), clock=self.clock)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.api = HttpClient(f"http://127.0.0.1:{self.port}")

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def test_health(self):
        status, body = self.api.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_write_requires_actor(self):
        status, body = self.api.request("POST", "/applications/submissions",
                                        valid_payload())
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "actor_required")

    def test_full_flow_over_http(self):
        # 1. 提交
        status, submitted = self.api.request(
            "POST", "/applications/submissions", valid_payload(), actor="fleet-clerk")
        self.assertEqual(status, 201)
        self.assertEqual(submitted["version"], 1)

        # 2. 重复提交（同内容）
        status, duplicate = self.api.request(
            "POST", "/applications/submissions", valid_payload(), actor="fleet-clerk")
        self.assertEqual(status, 201)
        self.assertTrue(duplicate["duplicate"])

        # 3. 辖区开审（规则全过）并批准
        status, opened = self.api.request(
            "POST", "/reviews",
            {"application_id": "AP-1", "version": 1, "jurisdiction": "JINGHAI"},
            actor="jh-reviewer")
        self.assertEqual(status, 201)
        review_id = opened["review_id"]
        status, approved = self.api.request(
            "POST", f"/reviews/{review_id}/approve",
            {"conditions": ["雨天限速30"]}, actor="jh-reviewer")
        self.assertEqual(status, 200)
        self.assertEqual(approved["state"], "approved")

        # 4. 签发
        status, issued = self.api.request(
            "POST", "/permits/issue",
            {"application_id": "AP-1", "version": 1}, actor="jh-director")
        self.assertEqual(status, 201)
        permit_id = issued["permit_id"]

        # 5. 另一车队同时段同路段 -> 409
        other = valid_payload(application_id="AP-2", fleet_id="FLEET-B")
        self.api.request("POST", "/applications/submissions", other, actor="fleet-b")
        status, opened_b = self.api.request(
            "POST", "/reviews",
            {"application_id": "AP-2", "version": 1, "jurisdiction": "JINGHAI"},
            actor="jh-reviewer")
        self.api.request("POST", f"/reviews/{opened_b['review_id']}/approve",
                         {}, actor="jh-reviewer")
        status, err = self.api.request(
            "POST", "/permits/issue",
            {"application_id": "AP-2", "version": 1}, actor="jh-director")
        self.assertEqual(status, 409)
        self.assertEqual(err["details"]["conflicts"][0]["type"], "permit_overlap")

        # 6. 查许可详情：责任人与审批依据齐备
        status, detail = self.api.request("GET", f"/permits/{permit_id}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["issued_by"], "jh-director")
        self.assertEqual(detail["review_basis"][0]["jurisdiction"], "JINGHAI")
        self.assertEqual(detail["review_basis"][0]["conditions"], ["雨天限速30"])

        # 7. 暂停
        status, _ = self.api.request(
            "POST", f"/permits/{permit_id}/suspend",
            {"reason": "事故调查"}, actor="jh-director")
        self.assertEqual(status, 200)
        status, detail = self.api.request("GET", f"/permits/{permit_id}")
        self.assertEqual(detail["status"], "suspended")

        # 8. 跨区互认（暂停期间应被拒绝）；恢复后在第 9b 步登记互认
        status, err = self.api.request(
            "POST", "/recognitions",
            {"permit_id": permit_id, "recognizing_jurisdiction": "JIADING",
             "scope": {}, "basis": "互认协议"}, actor="jd-observer")
        self.assertEqual(status, 409)
        self.api.request("POST", f"/permits/{permit_id}/reinstate",
                         {}, actor="jh-director")

        # 9. 临时封路 → 冲突 → 改期
        status, closure = self.api.request(
            "POST", "/road-closures",
            {"segment_codes": ["R-JH-01"], "starts_at": iso(BASE),
             "ends_at": iso(BASE + timedelta(hours=1)), "reason": "活动安保"},
            actor="traffic-police")
        self.assertEqual(status, 201)
        self.assertEqual(len(closure["conflict_ids"]), 1)
        conflict_id = closure["conflict_ids"][0]

        status, conflicts = self.api.request("GET", "/conflicts?state=open")
        self.assertEqual(status, 200)
        self.assertEqual(len(conflicts["conflicts"]), 1)

        new_start = BASE + timedelta(hours=3)
        status, resolved = self.api.request(
            "POST", f"/conflicts/{conflict_id}/resolve",
            {"resolution": "rescheduled",
             "new_window": window("R-JH-01", new_start),
             "note": "避让安保时段"}, actor="fleet-clerk")
        self.assertEqual(status, 200)
        self.assertEqual(resolved["state"], "rescheduled")

        # 9b. 改期后跨区互认按实际安排登记
        status, rec = self.api.request(
            "POST", "/recognitions",
            {"permit_id": permit_id, "recognizing_jurisdiction": "JIADING",
             "scope": {"segment_codes": ["R-JH-01"], "vehicle_ids": ["V-1"]},
             "basis": "长三角互认协议第3条"}, actor="jd-observer")
        self.assertEqual(status, 201)

        # 10. 时点还原：封路开始时，原时段没有许可占用；改期后时段有效
        self.clock.set(BASE + timedelta(minutes=30))
        status, snap = self.api.request(
            "GET", f"/snapshot?at={urllib.parse.quote(iso(self.clock.now()))}")
        self.assertEqual(status, 200)
        self.assertEqual(snap["effective_permits"], [])

        self.clock.set(new_start + timedelta(minutes=30))
        status, snap = self.api.request(
            "GET", f"/snapshot?at={urllib.parse.quote(iso(self.clock.now()))}")
        self.assertEqual(status, 200)
        self.assertEqual(len(snap["effective_permits"]), 1)
        row = snap["effective_permits"][0]
        self.assertEqual(row["route_windows"][0]["starts_at"], iso(new_start))
        self.assertEqual(row["recognitions"][0]["jurisdiction"], "JIADING")

        # 11. 申请历史：重复提交留痕、单一有效许可
        status, history = self.api.request("GET", "/applications/AP-1/history")
        self.assertEqual(status, 200)
        self.assertEqual(len(history["submissions"]), 2)
        self.assertTrue(history["submissions"][1]["duplicate_of"])
        self.assertEqual(len(history["permits"]), 1)

    def test_supplement_flow_over_http(self):
        expired = {
            "valid_from": iso(BASE - timedelta(days=30)),
            "valid_to": iso(BASE - timedelta(days=1)),
        }
        payload = valid_payload(vehicles={
            "V-1": {"plate": "沪D10001",
                    "qualifications": [{"type": "road_test_qualification", **expired}],
                    "insurance": {"policy_no": "INS-1",
                                  "valid_from": iso(BASE - timedelta(days=10)),
                                  "valid_to": iso(BASE + timedelta(days=30))}}})
        self.api.request("POST", "/applications/submissions", payload, actor="fleet-clerk")
        status, opened = self.api.request(
            "POST", "/reviews",
            {"application_id": "AP-1", "version": 1, "jurisdiction": "JINGHAI"},
            actor="jh-reviewer")
        review_id = opened["review_id"]
        self.assertTrue(opened["findings"])

        # 未补件批准 → 409
        status, err = self.api.request(
            "POST", f"/reviews/{review_id}/approve", {}, actor="jh-reviewer")
        self.assertEqual(status, 409)

        # 补件解决
        new_qual = {"valid_from": iso(BASE - timedelta(days=1)),
                    "valid_to": iso(BASE + timedelta(days=60))}
        status, _ = self.api.request(
            "POST", f"/reviews/{review_id}/supplements",
            {"documents": [{"document_id": "DOC-1",
                            "type": "road_test_qualification", **new_qual}],
             "resolved_finding_codes": ["VEHICLE_QUALIFICATION_EXPIRED"]},
            actor="fleet-clerk")
        self.assertEqual(status, 201)
        status, approved = self.api.request(
            "POST", f"/reviews/{review_id}/approve", {}, actor="jh-reviewer")
        self.assertEqual(status, 200)
        self.assertEqual(approved["state"], "approved")


if __name__ == "__main__":
    unittest.main()
