"""路段占用冲突与临时封路改期。"""

import unittest
from datetime import datetime, timedelta, timezone

from helpers import (
    approve_to_issuance,
    build_service,
    create_and_submit,
    fleet_actor,
    police_actor,
    reviewer_actor,
    safety_actor,
    window,
)
from permit_coordination.errors import (
    ConflictError,
    OccupancyConflictError,
    RuleViolationError,
    WorkflowError,
)
from permit_coordination.event_store import Actor

T0 = datetime(2026, 10, 21, 1, 0, tzinfo=timezone.utc)
CLOSURE_T0 = datetime(2026, 10, 21, 2, 0, tzinfo=timezone.utc)
AT_CLOSURE_WATCH = datetime(2026, 10, 21, 1, 30, tzinfo=timezone.utc)


def closure(segment="R-S1", start=CLOSURE_T0, hours=2.0, closure_id="CL-1"):
    return {
        "closure_id": closure_id,
        "segment_code": segment,
        "starts_at": start.isoformat(),
        "ends_at": (start + timedelta(hours=hours)).isoformat(),
        "reason": "大会开幕式警卫任务",
    }


class OccupancyTests(unittest.TestCase):
    def setUp(self):
        self.service, self.store, self.clock = build_service()
        create_and_submit(self.service, "AP-A")
        approve_to_issuance(self.service, "AP-A")

    def test_overlapping_window_on_other_permit_is_rejected(self):
        create_and_submit(
            self.service,
            "AP-B",
            route_windows=[
                window(segment="R-S1", start=T0.replace(hour=2, minute=30), hours=1.0)
            ],
        )
        self.service.record_rule_check("AP-B", reviewer_actor())
        self.service.countersign("AP-B", "ok", safety_actor())
        self.service.countersign("AP-B", "ok", police_actor())
        with self.assertRaises(OccupancyConflictError) as ctx:
            self.service.issue_permit("AP-B", reviewer_actor())
        self.assertEqual(ctx.exception.conflicts[0]["code"], "permit_overlap")
        self.assertEqual(ctx.exception.conflicts[0]["other_application_id"], "AP-A")

    def test_adjacent_half_open_windows_do_not_conflict(self):
        # AP-A: 01:00-05:00，AP-B: 05:00-06:00 首尾相接，半开区间不冲突
        create_and_submit(
            self.service,
            "AP-B",
            route_windows=[window(segment="R-S1", start=T0.replace(hour=5), hours=1.0)],
        )
        approve_to_issuance(self.service, "AP-B")
        self.assertEqual(self.service.get_application_view("AP-B")["state"], "signed")

    def test_issuing_into_an_active_road_closure_is_rejected(self):
        self.service.register_closure(
            closure(start=T0.replace(hour=0), hours=8.0, closure_id="CL-PRE"),
            reviewer_actor(),
        )
        create_and_submit(self.service, "AP-C")
        self.service.record_rule_check("AP-C", reviewer_actor())
        self.service.countersign("AP-C", "ok", safety_actor())
        self.service.countersign("AP-C", "ok", police_actor())
        with self.assertRaises(OccupancyConflictError) as ctx:
            self.service.issue_permit("AP-C", reviewer_actor())
        self.assertEqual(ctx.exception.conflicts[0]["code"], "road_closure")
        self.assertEqual(ctx.exception.conflicts[0]["closure_id"], "CL-PRE")


class ClosureFlowTests(unittest.TestCase):
    def setUp(self):
        self.service, self.store, self.clock = build_service()
        create_and_submit(self.service, "AP-A")
        approve_to_issuance(self.service, "AP-A")
        self.clock.set(AT_CLOSURE_WATCH)

    def test_closure_opens_tracked_conflict(self):
        result = self.service.register_closure(closure(), reviewer_actor())
        self.assertEqual(result["conflict_count"], 1)
        cid = result["conflict_ids"][0]
        view = self.service.get_conflict_view(cid)
        self.assertEqual(view["status"], "open")
        self.assertEqual(view["application_id"], "AP-A")
        self.assertEqual(view["segment_code"], "R-S1")

    def test_duplicate_closure_registration_is_rejected(self):
        self.service.register_closure(closure(), reviewer_actor())
        with self.assertRaises(ConflictError):
            self.service.register_closure(closure(), reviewer_actor())

    def test_reschedule_full_flow_amends_permit(self):
        cid = self.service.register_closure(closure(), reviewer_actor())["conflict_ids"][0]
        proposal = self.service.propose_resolution(cid, {}, reviewer_actor())  # 系统自动改期
        self.assertEqual(len(proposal["added_windows"]), 1)
        auto_start = proposal["added_windows"][0]["starts_at"]
        # 自动改期起点为封路结束（04:00）
        self.assertEqual(
            auto_start, T0.replace(hour=4).isoformat()
        )
        self.service.respond_resolution(cid, True, "同意改期", fleet_actor())
        self.service.countersign_resolution(cid, "交通组织确认", police_actor())
        result = self.service.close_conflict(cid, reviewer_actor())
        self.assertEqual(result["outcome"], "rescheduled")
        conflict = self.service.get_conflict_view(cid)
        self.assertEqual(conflict["status"], "resolved_rescheduled")

        effective = self.service.effective_view("AP-A", self.clock.now())
        windows = effective["active_route_windows"]
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["starts_at"], auto_start)
        # 旧 grant 已失效并指向新 grant
        invalid = [
            g
            for g in self.service.get_application_view("AP-A")["grants"]
            if g["invalidated"] and g["invalidated"]["conflict_id"] == cid
        ]
        self.assertEqual(len(invalid), 1)
        self.assertEqual(invalid[0]["invalidated"]["reason"], "road_closure_reschedule")
        # 许可修正事件可追溯
        amendments = self.service.get_application_view("AP-A")["amendments"]
        self.assertEqual(amendments[-1]["conflict_id"], cid)

    def test_rescheduled_permit_remains_consistent_under_as_of_queries(self):
        cid = self.service.register_closure(closure(), reviewer_actor())["conflict_ids"][0]
        before_amend = self.clock.now()
        self.clock.advance(minutes=15)
        self.service.propose_resolution(cid, {}, reviewer_actor())
        self.clock.advance(minutes=15)
        self.service.respond_resolution(cid, True, "同意", fleet_actor())
        self.clock.advance(minutes=15)
        self.service.countersign_resolution(cid, "确认", police_actor())
        self.clock.advance(minutes=15)
        self.service.close_conflict(cid, reviewer_actor())
        # 改期前的时点：旧时窗仍有效
        old = self.service.effective_view("AP-A", before_amend)
        self.assertEqual(len(old["active_route_windows"]), 1)
        self.assertEqual(old["active_route_windows"][0]["starts_at"], T0.isoformat())
        # 当前时点：只有改期后的时窗
        now_view = self.service.effective_view("AP-A", self.clock.now())
        self.assertEqual(now_view["active_route_windows"][0]["starts_at"], T0.replace(hour=4).isoformat())

    def test_rejected_resolution_cancels_window(self):
        cid = self.service.register_closure(closure(), reviewer_actor())["conflict_ids"][0]
        self.service.propose_resolution(cid, {}, reviewer_actor())
        self.service.respond_resolution(cid, False, "当天无替补人员", fleet_actor())
        result = self.service.close_conflict(cid, reviewer_actor())
        self.assertEqual(result["outcome"], "cancelled")
        effective = self.service.effective_view("AP-A", self.clock.now())
        self.assertEqual(effective["active_route_windows"], [])
        grants = self.service.get_application_view("AP-A")["grants"]
        invalid = [g for g in grants if g["status"] == "invalidated"]
        self.assertTrue(
            any(g["invalidated"]["reason"] == "road_closure_cancelled" for g in invalid)
        )

    def test_manual_proposal_still_inside_closure_is_rejected(self):
        cid = self.service.register_closure(closure(), reviewer_actor())["conflict_ids"][0]
        bad = {
            "added_windows": [
                window(segment="R-S1", start=T0.replace(hour=3), hours=0.5)
            ]
        }
        with self.assertRaises(RuleViolationError) as ctx:
            self.service.propose_resolution(cid, bad, reviewer_actor())
        self.assertIn("still_closure", {f["code"] for f in ctx.exception.findings})

    def test_cannot_close_before_applicant_response(self):
        cid = self.service.register_closure(closure(), reviewer_actor())["conflict_ids"][0]
        self.service.propose_resolution(cid, {}, reviewer_actor())
        with self.assertRaises(WorkflowError):
            self.service.close_conflict(cid, reviewer_actor())

    def test_countersign_requires_applicant_acceptance(self):
        cid = self.service.register_closure(closure(), reviewer_actor())["conflict_ids"][0]
        self.service.propose_resolution(cid, {}, reviewer_actor())
        with self.assertRaises(WorkflowError):
            self.service.countersign_resolution(cid, "抢跑", police_actor())

    def test_reschedule_into_another_window_of_same_permit_is_rejected(self):
        # 新许可 AP-DUAL：R-S1 与 R-S2 两个时窗；把被封的 R-S1 改到与 R-S2 重叠必须拒绝
        day = T0.replace(day=25)
        create_and_submit(
            self.service,
            "AP-DUAL",
            route_windows=[
                window(segment="R-S1", start=day, hours=4.0),
                window(segment="R-S2", start=day.replace(hour=4), hours=2.0),
            ],
        )
        approve_to_issuance(self.service, "AP-DUAL")
        self.clock.set(day.replace(hour=1, minute=30))
        cid = self.service.register_closure(
            closure(start=day.replace(hour=2), closure_id="CL-DUAL"), reviewer_actor()
        )["conflict_ids"][0]
        # 冲突应只来自 AP-DUAL（AP-A 的时窗在 21 日）
        conflict = self.service.get_conflict_view(cid)
        self.assertEqual(conflict["application_id"], "AP-DUAL")
        bad = {"added_windows": [window(segment="R-S2", start=day.replace(hour=4), hours=2.0)]}
        with self.assertRaises(RuleViolationError) as ctx:
            self.service.propose_resolution(cid, bad, reviewer_actor())
        self.assertIn("self_permit_overlap", {f["code"] for f in ctx.exception.findings})


class SubmissionWindowShapeTests(unittest.TestCase):
    def setUp(self):
        self.service, self.store, self.clock = build_service()

    def test_self_overlapping_windows_in_one_version_are_rejected(self):
        from helpers import standard_payload
        from permit_coordination.errors import ValidationError

        create_and_submit(self.service, "AP-S")
        payload = standard_payload(
            route_windows=[
                window(segment="R-S1", start=T0, hours=3.0),
                window(segment="R-S1", start=T0.replace(hour=2), hours=2.0),
            ]
        )
        with self.assertRaises(ValidationError) as ctx:
            self.service.submit_version("AP-S", payload, fleet_actor())
        self.assertEqual(ctx.exception.details["segment_code"], "R-S1")


class ConcurrentIssuanceTests(unittest.TestCase):
    def setUp(self):
        self.service, self.store, self.clock = build_service()

    def test_concurrent_issuance_of_overlapping_windows_allows_exactly_one(self):
        import threading

        # 两支车队申请同一时窗，都已完成审查，只差签发
        for app_id in ("AP-T1", "AP-T2"):
            create_and_submit(self.service, app_id)
            self.service.record_rule_check(app_id, reviewer_actor())
            self.service.countersign(app_id, "s", safety_actor())
            self.service.countersign(app_id, "p", police_actor())

        results: dict[str, object] = {}
        barrier = threading.Barrier(2)

        def issue(app_id: str) -> None:
            barrier.wait()
            try:
                results[app_id] = self.service.issue_permit(app_id, reviewer_actor())
            except OccupancyConflictError as exc:
                results[app_id] = exc

        threads = [
            threading.Thread(target=issue, args=("AP-T1",)),
            threading.Thread(target=issue, args=("AP-T2",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        successes = [v for v in results.values() if not isinstance(v, OccupancyConflictError)]
        conflicts = [v for v in results.values() if isinstance(v, OccupancyConflictError)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].conflicts[0]["code"], "permit_overlap")


if __name__ == "__main__":
    unittest.main()
