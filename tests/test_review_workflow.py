"""审查工作流：补件、规则校验、会签前置条件。"""

import unittest
from datetime import timedelta

from helpers import (
    BASE,
    approve_to_issuance,
    build_service,
    create_and_submit,
    fleet_actor,
    reviewer_actor,
    safety_actor,
    police_actor,
    standard_payload,
    vehicle,
    window,
)
from permit_coordination.errors import RuleViolationError, ValidationError, WorkflowError


class ReviewWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.service, self.store, self.clock = build_service()
        create_and_submit(self.service)

    def test_expired_vehicle_is_a_blocker(self):
        payload = standard_payload(
            vehicles=[vehicle(valid_until=BASE - timedelta(days=1))]
        )
        self.service.submit_version("AP-1", payload, fleet_actor())
        result = self.service.record_rule_check("AP-1", reviewer_actor())
        codes = {f["code"] for f in result["findings"]}
        self.assertIn("vehicle_expired", codes)
        self.assertGreaterEqual(result["blocker_count"], 1)
        with self.assertRaises(RuleViolationError):
            self.service.countersign("AP-1", "同意", safety_actor())

    def test_window_longer_than_jurisdiction_limit_blocks_signoff(self):
        payload = standard_payload(
            route_windows=[window(start=BASE + timedelta(days=1), hours=12)]
        )
        self.service.submit_version("AP-1", payload, fleet_actor())
        result = self.service.record_rule_check("AP-1", reviewer_actor())
        self.assertIn("window_too_long", {f["code"] for f in result["findings"]})

    def test_segment_outside_jurisdiction_is_blocked(self):
        payload = standard_payload(
            route_windows=[window(segment="R-UNKNOWN", start=BASE + timedelta(days=1))]
        )
        self.service.submit_version("AP-1", payload, fleet_actor())
        result = self.service.record_rule_check("AP-1", reviewer_actor())
        self.assertIn("segment_not_allowed", {f["code"] for f in result["findings"]})

    def test_missing_required_jurisdiction_capability_is_blocker(self):
        from helpers import capability

        payload = standard_payload(
            capabilities=[capability(code="valet_parking_only")]
        )
        # 人车能力也需同步调整，否则会产生其他 blocker；这里只需确认 required_capability_missing
        self.service.submit_version("AP-1", payload, fleet_actor())
        result = self.service.record_rule_check("AP-1", reviewer_actor())
        self.assertIn(
            "required_capability_missing", {f["code"] for f in result["findings"]}
        )

    def test_info_request_moves_state_and_questions_are_recorded(self):
        result = self.service.request_info(
            "AP-1", ["请补充保险凭证", "请说明跟车距离策略"], reviewer_actor()
        )
        self.assertTrue(result["info_request_event_id"])
        view = self.service.get_application_view("AP-1")
        self.assertEqual(view["state"], "info_requested")
        self.assertEqual(len(view["info_requests"]), 1)
        self.assertEqual(view["info_requests"][-1]["questions"][0], "请补充保险凭证")

    def test_info_request_requires_real_questions(self):
        with self.assertRaises(ValidationError):
            self.service.request_info("AP-1", ["  "], reviewer_actor())

    def test_countersign_requires_rule_check_first(self):
        with self.assertRaises(WorkflowError):
            self.service.countersign("AP-1", "同意", safety_actor())

    def test_unknown_role_cannot_countersign(self):
        self.service.record_rule_check("AP-1", reviewer_actor())
        from permit_coordination.event_store import Actor

        rogue = Actor("U-X", "janitor", "SH_DEMO")
        with self.assertRaises(WorkflowError):
            self.service.countersign("AP-1", "我不管我同意", rogue)

    def test_issue_requires_all_countersignatures(self):
        self.service.record_rule_check("AP-1", reviewer_actor())
        self.service.countersign("AP-1", "安全通过", safety_actor())
        with self.assertRaises(WorkflowError) as ctx:
            self.service.issue_permit("AP-1", reviewer_actor())
        self.assertEqual(ctx.exception.details["missing_roles"], ["traffic_police"])

    def test_double_countersign_same_version_is_rejected(self):
        self.service.record_rule_check("AP-1", reviewer_actor())
        self.service.countersign("AP-1", "安全通过", safety_actor())
        from permit_coordination.errors import ConflictError

        with self.assertRaises(ConflictError):
            self.service.countersign("AP-1", "重复签", safety_actor())

    def test_full_review_then_issue(self):
        result = approve_to_issuance(self.service)
        self.assertTrue(result["permit_number"].startswith("SH_DEMO-P-"))
        view = self.service.get_application_view("AP-1")
        self.assertEqual(view["state"], "signed")
        self.assertEqual(len(view["countersignatures"]), 2)
        self.assertEqual(view["issues"][0]["rules_version"], "2026.09")


if __name__ == "__main__":
    unittest.main()
