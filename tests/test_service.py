"""应用服务层测试：版本、幂等、审查补件、会签签发、暂停撤销、互认、封路冲突、时点还原。"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from permit_coordination.errors import ConflictError, NotFoundError, ValidationError
from tests._helpers import BASE, approve_jurisdictions, build_service, cover, iso, valid_payload, window


class SubmissionTests(unittest.TestCase):
    def test_first_submission_is_version_1(self):
        service, _ = build_service()
        result = service.submit_application(valid_payload(), actor="fleet-clerk")
        self.assertEqual(result["version"], 1)
        self.assertFalse(result["duplicate"])

    def test_identical_resubmit_is_recorded_but_duplicate(self):
        service, _ = build_service()
        first = service.submit_application(valid_payload(), actor="fleet-clerk")
        again = service.submit_application(valid_payload(), actor="fleet-clerk")
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["duplicate_of"], first["submission_id"])
        self.assertEqual(again["version"], 1)
        # 历史中两条都留痕，但有效版本只有一个
        history = service.history("AP-1")
        self.assertEqual(len(history["submissions"]), 2)
        actives = service.projection.active_submissions("AP-1")
        self.assertEqual(len(actives), 1)
        self.assertEqual(actives[0].submission_id, first["submission_id"])

    def test_idempotency_key_returns_same_submission(self):
        service, _ = build_service()
        first = service.submit_application(
            valid_payload(idempotency_key="K-1"), actor="fleet-clerk")
        # 即使内容改了，同一幂等键也直接返回原提交，不产生新事件
        changed = valid_payload(idempotency_key="K-1",
                                vehicles={**valid_payload()["vehicles"],
                                          "V-2": valid_payload()["vehicles"]["V-1"]})
        again = service.submit_application(changed, actor="fleet-clerk")
        self.assertEqual(again["submission_id"], first["submission_id"])
        self.assertTrue(again["duplicate"])
        self.assertEqual(len(service.projection.submissions), 1)

    def test_revision_increments_version_and_reports_changes(self):
        service, _ = build_service()
        service.submit_application(valid_payload(), actor="fleet-clerk")
        revised = valid_payload(
            vehicles={**valid_payload()["vehicles"],
                      "V-2": valid_payload()["vehicles"]["V-1"]},
            route_windows=[window("R-JH-01", BASE), window("R-JH-02", BASE + timedelta(hours=4))])
        result = service.submit_application(revised, actor="fleet-clerk")
        self.assertEqual(result["version"], 2)
        self.assertIn("V-2", result["changed_vehicles"])
        self.assertIn("R-JH-02", result["changed_segments"])


class ReviewTests(unittest.TestCase):
    def test_expired_qualification_blocks_approval_until_supplemented(self):
        service, _ = build_service()
        expired = {
            "valid_from": iso(BASE - timedelta(days=30)),
            "valid_to": iso(BASE - timedelta(days=1)),
        }
        payload = valid_payload(vehicles={
            "V-1": {"plate": "沪D10001",
                    "qualifications": [{"type": "road_test_qualification", **expired}],
                    "insurance": {"policy_no": "INS-1", **cover()}}})
        service.submit_application(payload, actor="fleet-clerk")
        opened = service.open_review("AP-1", 1, "JINGHAI", actor="jh-reviewer")
        codes = {f["code"] for f in opened["findings"]}
        self.assertIn("VEHICLE_QUALIFICATION_EXPIRED", codes)

        review_id = opened["review_id"]
        with self.assertRaises(ConflictError):
            service.approve_review(review_id, actor="jh-reviewer")

        # 补上新资质
        service.submit_supplement(
            review_id,
            [{"document_id": "DOC-Q1", "type": "road_test_qualification", **cover()}],
            ["VEHICLE_QUALIFICATION_EXPIRED"], actor="fleet-clerk")
        approved = service.approve_review(review_id, actor="jh-reviewer")
        self.assertEqual(approved["state"], "approved")

        detail = service.review_detail(review_id)
        finding = next(f for f in detail["findings"]
                       if f["code"] == "VEHICLE_QUALIFICATION_EXPIRED")
        self.assertTrue(finding["resolved"])
        self.assertEqual(finding["resolved_by"], "fleet-clerk")

    def test_wrong_jurisdiction_segment_is_flagged(self):
        service, _ = build_service()
        payload = valid_payload(route_windows=[window("R-JD-09", BASE)])
        service.submit_application(payload, actor="fleet-clerk")
        opened = service.open_review("AP-1", 1, "JINGHAI", actor="jh-reviewer")
        self.assertTrue(any(f["code"] == "SEGMENT_NOT_ALLOWED" for f in opened["findings"]))

    def test_capability_level_rule(self):
        service, _ = build_service()
        payload = valid_payload(capability={"level": "L2", "functions": [], "certifications": []})
        service.submit_application(payload, actor="fleet-clerk")
        opened = service.open_review("AP-1", 1, "JINGHAI", actor="jh-reviewer")
        codes = {f["code"] for f in opened["findings"]}
        self.assertIn("CAPABILITY_LEVEL_INSUFFICIENT", codes)
        self.assertIn("CAPABILITY_FUNCTION_MISSING", codes)

    def test_duplicate_open_review_is_idempotent(self):
        service, _ = build_service()
        service.submit_application(valid_payload(), actor="fleet-clerk")
        a = service.open_review("AP-1", 1, "JINGHAI", actor="jh-reviewer")
        b = service.open_review("AP-1", 1, "JINGHAI", actor="jh-reviewer")
        self.assertEqual(a["review_id"], b["review_id"])
        self.assertTrue(b["duplicate"])


class IssuanceTests(unittest.TestCase):
    def test_full_countersign_and_issue(self):
        service, _ = build_service()
        payload = valid_payload(
            route_windows=[window("R-JH-01", BASE), window("R-JD-01", BASE)])
        service.submit_application(payload, actor="fleet-clerk")
        approve_jurisdictions(service, "AP-1", 1, ("JINGHAI", "JIADING"))
        result = service.issue_permit("AP-1", 1, actor="jh-director")
        self.assertTrue(result["permit_id"].startswith("PMT-"))
        self.assertEqual(set(result["approving_jurisdictions"]), {"JINGHAI", "JIADING"})

        detail = service.permit_detail(result["permit_id"])
        self.assertEqual(detail["issued_by"], "jh-director")
        self.assertEqual(len(detail["review_basis"]), 2)
        basis_actors = {b["jurisdiction"]: b["decided_by"] for b in detail["review_basis"]}
        self.assertEqual(basis_actors["JINGHAI"], "jinghai-reviewer")
        self.assertEqual(basis_actors["JIADING"], "jiading-reviewer")

    def test_issue_requires_all_jurisdictions(self):
        service, _ = build_service()
        payload = valid_payload(route_windows=[window("R-JH-01", BASE), window("R-JD-01", BASE)])
        service.submit_application(payload, actor="fleet-clerk")
        approve_jurisdictions(service, "AP-1", 1, ("JINGHAI",))
        with self.assertRaises(ConflictError) as ctx:
            service.issue_permit("AP-1", 1, actor="jh-director")
        self.assertIn("JIADING", ctx.exception.details["missing_jurisdictions"])

    def test_duplicate_submission_does_not_create_second_permit(self):
        service, _ = build_service()
        first = service.submit_application(valid_payload(), actor="fleet-clerk")
        service.submit_application(valid_payload(), actor="fleet-clerk")  # 重复
        approve_jurisdictions(service, "AP-1", 1, ("JINGHAI",))
        issued = service.issue_permit("AP-1", 1, actor="jh-director")
        self.assertEqual(len(service.projection.permits), 1)
        # 再次签发同版本 -> 最新有效版本仍是 v1，但同申请同版本已签发：
        # 通过历史只能看到一份许可
        history = service.history("AP-1")
        self.assertEqual(len(history["permits"]), 1)
        self.assertEqual(history["permits"][0]["permit_id"], issued["permit_id"])
        self.assertEqual(first["version"], 1)

    def test_overlapping_windows_between_fleets_are_rejected(self):
        service, clock = build_service()
        service.submit_application(valid_payload(), actor="fleet-a")
        approve_jurisdictions(service, "AP-1", 1, ("JINGHAI",))
        service.issue_permit("AP-1", 1, actor="jh-director")

        other = valid_payload(application_id="AP-2", fleet_id="FLEET-B")
        service.submit_application(other, actor="fleet-b")
        approve_jurisdictions(service, "AP-2", 1, ("JINGHAI",))
        with self.assertRaises(ConflictError) as ctx:
            service.issue_permit("AP-2", 1, actor="jh-director")
        self.assertEqual(ctx.exception.details["conflicts"][0]["type"], "permit_overlap")


class SupersedeTests(unittest.TestCase):
    def test_revision_issue_explicitly_supersedes_old_permit_with_changed_items(self):
        service, clock = build_service()
        service.submit_application(valid_payload(), actor="fleet-clerk")
        approve_jurisdictions(service, "AP-1", 1, ("JINGHAI",))
        first = service.issue_permit("AP-1", 1, actor="jh-director")

        # 车辆变更 → 修订版本 v2
        revised = valid_payload(vehicles={
            "V-1": valid_payload()["vehicles"]["V-1"],
            "V-9": valid_payload()["vehicles"]["V-1"],
        })
        service.submit_application(revised, actor="fleet-clerk")
        approve_jurisdictions(service, "AP-1", 2, ("JINGHAI",))
        second = service.issue_permit("AP-1", 2, actor="jh-director")

        self.assertEqual(second["superseded_permit_ids"], [first["permit_id"]])
        old = service.permit_detail(first["permit_id"])
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(old["replaced_by_permit_id"], second["permit_id"])
        self.assertIn("V-9", old["changed_vehicles"])

        new = service.permit_detail(second["permit_id"])
        self.assertEqual(new["status"], "issued")
        self.assertEqual(set(new["vehicle_ids"]), {"V-1", "V-9"})

    def test_cannot_issue_for_superseded_version(self):
        service, _ = build_service()
        service.submit_application(valid_payload(), actor="fleet-clerk")
        approve_jurisdictions(service, "AP-1", 1, ("JINGHAI",))
        service.issue_permit("AP-1", 1, actor="jh-director")
        # v2 修订
        service.submit_application(
            valid_payload(route_windows=[window("R-JH-07", BASE)]), actor="fleet-clerk")
        with self.assertRaises(ConflictError):
            service.issue_permit("AP-1", 1, actor="jh-director")


class SuspendRevokeTests(unittest.TestCase):
    def _issued(self):
        service, clock = build_service()
        service.submit_application(valid_payload(), actor="fleet-clerk")
        approve_jurisdictions(service, "AP-1", 1, ("JINGHAI",))
        service.issue_permit("AP-1", 1, actor="jh-director")
        return service, clock

    def test_suspend_reinstate_cycle(self):
        service, clock = self._issued()
        permit_id = service.history("AP-1")["permits"][0]["permit_id"]
        service.suspend_permit(permit_id, "天气管制", actor="jh-director")
        self.assertEqual(service.permit_detail(permit_id)["status"], "suspended")
        service.reinstate_permit(permit_id, actor="jh-director")
        self.assertEqual(service.permit_detail(permit_id)["status"], "issued")

    def test_revoked_permit_blocks_lifecycle_actions(self):
        service, _ = self._issued()
        permit_id = service.history("AP-1")["permits"][0]["permit_id"]
        service.revoke_permit(permit_id, "资质造假", actor="jh-director")
        with self.assertRaises(ConflictError):
            service.suspend_permit(permit_id, "x", actor="jh-director")
        with self.assertRaises(ConflictError):
            service.reinstate_permit(permit_id, actor="jh-director")

    def test_reinstate_rejected_when_segment_reassigned_while_suspended(self):
        service, clock = self._issued()
        first_id = service.history("AP-1")["permits"][0]["permit_id"]
        service.suspend_permit(first_id, "事故调查", actor="jh-director")

        # 暂停期间，另一车队拿到同路段同时窗许可
        other = valid_payload(application_id="AP-2", fleet_id="FLEET-B")
        service.submit_application(other, actor="fleet-b")
        approve_jurisdictions(service, "AP-2", 1, ("JINGHAI",))
        service.issue_permit("AP-2", 1, actor="jh-director")

        with self.assertRaises(ConflictError) as ctx:
            service.reinstate_permit(first_id, actor="jh-director")
        self.assertEqual(
            ctx.exception.details["conflicts"][0]["type"], "permit_overlap")

    def test_expired_permit_not_listed_but_revocable_history_kept(self):
        service, clock = self._issued()
        permit_id = service.history("AP-1")["permits"][0]["permit_id"]
        clock.set(BASE + timedelta(days=31))
        self.assertEqual(service.effective_permits(), [])
        detail = service.permit_detail(permit_id)
        self.assertEqual(detail["status"], "issued")  # 状态仍是签发，只是过了有效期


class RecognitionTests(unittest.TestCase):
    def _issued(self):
        service, clock = build_service()
        service.submit_application(valid_payload(), actor="fleet-clerk")
        approve_jurisdictions(service, "AP-1", 1, ("JINGHAI",))
        result = service.issue_permit("AP-1", 1, actor="jh-director")
        return service, clock, result["permit_id"]

    def test_recognition_keeps_actual_recognized_scope(self):
        service, _, permit_id = self._issued()
        result = service.record_recognition(
            permit_id, "JIADING",
            {"vehicle_ids": ["V-1"], "segment_codes": ["R-JH-01"]},
            basis="长三角互认协议第3条", actor="jd-observer")
        detail = service.recognition_detail(result["recognition_id"])
        self.assertEqual(detail["vehicle_ids"], ["V-1"])
        self.assertEqual(detail["segment_codes"], ["R-JH-01"])
        self.assertEqual(detail["state"], "active")

    def test_recognition_scope_cannot_exceed_permit(self):
        service, _, permit_id = self._issued()
        with self.assertRaises(ValidationError):
            service.record_recognition(
                permit_id, "JIADING", {"vehicle_ids": ["V-999"]},
                basis="协议", actor="jd-observer")
        with self.assertRaises(ValidationError):
            service.record_recognition(
                permit_id, "JIADING", {"segment_codes": ["R-JH-99"]},
                basis="协议", actor="jd-observer")

    def test_narrower_recognition_supersedes_prior(self):
        service, _, permit_id = self._issued()
        first = service.record_recognition(
            permit_id, "JIADING", {}, basis="协议v1", actor="jd-observer")
        narrower = service.record_recognition(
            permit_id, "JIADING", {"segment_codes": ["R-JH-01"]},
            basis="协议v2-缩窄", actor="jd-observer")
        self.assertEqual(
            service.recognition_detail(first["recognition_id"])["state"], "superseded")
        self.assertEqual(
            service.recognition_detail(narrower["recognition_id"])["state"], "active")

    def test_recognition_revocation_is_traceable(self):
        service, _, permit_id = self._issued()
        rec = service.record_recognition(
            permit_id, "LIN_GANG", {}, basis="协议", actor="lg-observer")
        service.revoke_recognition(rec["recognition_id"], "发证辖区暂停许可",
                                   actor="lg-observer")
        detail = service.recognition_detail(rec["recognition_id"])
        self.assertEqual(detail["state"], "revoked")
        self.assertEqual(detail["revoked_reason"], "发证辖区暂停许可")
        self.assertEqual(detail["recorded_by"], "lg-observer")


class RoadClosureTests(unittest.TestCase):
    def _issued(self):
        service, clock = build_service()
        service.submit_application(valid_payload(), actor="fleet-clerk")
        approve_jurisdictions(service, "AP-1", 1, ("JINGHAI",))
        result = service.issue_permit("AP-1", 1, actor="jh-director")
        return service, clock, result["permit_id"]

    def test_closure_creates_trackable_conflict(self):
        service, clock, permit_id = self._issued()
        result = service.register_road_closure(
            ["R-JH-01"], iso(BASE), iso(BASE + timedelta(hours=1)),
            reason="活动管制", actor="traffic-police")
        self.assertEqual(len(result["conflict_ids"]), 1)
        conflict = service.conflict_detail(result["conflict_ids"][0])
        self.assertEqual(conflict["state"], "open")
        self.assertEqual(conflict["permit_id"], permit_id)

    def test_reschedule_replaces_window_on_permit(self):
        service, clock, permit_id = self._issued()
        closure = service.register_road_closure(
            ["R-JH-01"], iso(BASE), iso(BASE + timedelta(hours=1)),
            reason="管制", actor="traffic-police")
        conflict_id = closure["conflict_ids"][0]
        new_start = BASE + timedelta(hours=3)
        service.resolve_conflict(
            conflict_id, "rescheduled", actor="fleet-clerk",
            new_window=window("R-JH-01", new_start))
        conflict = service.conflict_detail(conflict_id)
        self.assertEqual(conflict["state"], "rescheduled")
        self.assertEqual(conflict["new_window"]["starts_at"], iso(new_start))
        windows = service.permit_detail(permit_id)["effective_windows"]
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["starts_at"], iso(new_start))

    def test_reschedule_into_another_closure_is_rejected(self):
        service, clock, permit_id = self._issued()
        closure = service.register_road_closure(
            ["R-JH-01"], iso(BASE), iso(BASE + timedelta(hours=1)),
            reason="管制", actor="traffic-police")
        # 另一道封路覆盖建议改期时间
        service.register_road_closure(
            ["R-JH-01"], iso(BASE + timedelta(hours=3)),
            iso(BASE + timedelta(hours=4)), reason="第二道管制", actor="traffic-police")
        with self.assertRaises(ConflictError):
            service.resolve_conflict(
                closure["conflict_ids"][0], "rescheduled", actor="fleet-clerk",
                new_window=window("R-JH-01", BASE + timedelta(hours=3)))

    def test_cancel_removes_window(self):
        service, clock, permit_id = self._issued()
        closure = service.register_road_closure(
            ["R-JH-01"], iso(BASE), iso(BASE + timedelta(hours=3)),
            reason="管制", actor="traffic-police")
        service.resolve_conflict(closure["conflict_ids"][0], "cancelled",
                                 actor="jh-director", note="当日不再测试")
        self.assertEqual(service.permit_detail(permit_id)["effective_windows"], [])
        self.assertEqual(service.list_conflicts(state="open"), [])

    def test_resolved_conflict_cannot_be_processed_twice(self):
        service, clock, _ = self._issued()
        closure = service.register_road_closure(
            ["R-JH-01"], iso(BASE), iso(BASE + timedelta(hours=1)),
            reason="管制", actor="traffic-police")
        cid = closure["conflict_ids"][0]
        service.resolve_conflict(cid, "cancelled", actor="jh-director")
        with self.assertRaises(ConflictError):
            service.resolve_conflict(cid, "rescheduled", actor="jh-director",
                                     new_window=window("R-JH-01", BASE + timedelta(hours=5)))


class SnapshotTests(unittest.TestCase):
    def test_as_of_reconstructs_permits_actors_and_basis(self):
        service, clock = build_service()
        service.submit_application(valid_payload(), actor="fleet-clerk")
        approve_jurisdictions(service, "AP-1", 1, ("JINGHAI",))
        issued_at = clock.now()
        result = service.issue_permit("AP-1", 1, actor="jh-director")
        permit_id = result["permit_id"]

        # 签发之前：没有有效许可
        before = service.snapshot_at(issued_at - timedelta(seconds=1))
        self.assertEqual(before["effective_permits"], [])

        # 测试时窗开始时：许可有效，责任人/依据可还原
        during = service.snapshot_at(BASE + timedelta(minutes=30))
        self.assertEqual(len(during["effective_permits"]), 1)
        row = during["effective_permits"][0]
        self.assertEqual(row["permit_id"], permit_id)
        self.assertEqual(row["issued_by"], "jh-director")
        self.assertEqual(row["review_basis"][0]["decided_by"], "jinghai-reviewer")

        # 暂停后：该时点不再有效
        clock.set(BASE + timedelta(hours=1))
        service.suspend_permit(permit_id, "事故调查", actor="jh-director")
        suspended = service.snapshot_at(clock.now() + timedelta(minutes=1))
        self.assertEqual(suspended["effective_permits"], [])

        # 但恢复后再看：同一许可重新有效
        service.reinstate_permit(permit_id, actor="jh-director")
        reinstated = service.snapshot_at(clock.now() + timedelta(minutes=1))
        self.assertEqual(len(reinstated["effective_permits"]), 1)

    def test_snapshot_includes_conflicts_as_of_time(self):
        service, clock = build_service()
        service.submit_application(valid_payload(), actor="fleet-clerk")
        approve_jurisdictions(service, "AP-1", 1, ("JINGHAI",))
        service.issue_permit("AP-1", 1, actor="jh-director")
        clock.set(BASE)
        closure = service.register_road_closure(
            ["R-JH-01"], iso(BASE), iso(BASE + timedelta(hours=1)),
            reason="管制", actor="traffic-police")
        cid = closure["conflict_ids"][0]

        at_open = BASE + timedelta(minutes=5)
        snap_open = service.snapshot_at(at_open)
        self.assertEqual(len(snap_open["conflicts"]), 1)
        self.assertEqual(snap_open["conflicts"][0]["state"], "open")

        clock.set(BASE + timedelta(minutes=6))
        service.resolve_conflict(
            cid, "cancelled", actor="jh-director", note="当日取消")
        snap_done = service.snapshot_at(BASE + timedelta(minutes=10))
        self.assertEqual(snap_done["conflicts"][0]["state"], "cancelled")
        self.assertEqual(snap_done["conflicts"][0]["resolved_by"], "jh-director")

        # 冲突产生之前的时点看不到冲突
        snap_before = service.snapshot_at(BASE - timedelta(hours=1))
        self.assertEqual(snap_before["conflicts"], [])

    def test_event_journal_is_complete_audit_trail(self):
        service, _ = build_service()
        service.submit_application(valid_payload(), actor="fleet-clerk")
        approve_jurisdictions(service, "AP-1", 1, ("JINGHAI",))
        service.issue_permit("AP-1", 1, actor="jh-director")
        journal = service.event_journal()
        types = [e["type"] for e in journal]
        self.assertIn("ApplicationSubmitted", types)
        self.assertIn("ReviewOpened", types)
        self.assertIn("ReviewApproved", types)
        self.assertIn("PermitIssued", types)
        for event in journal:
            self.assertTrue(event["actor"])
            self.assertTrue(event["at"])


if __name__ == "__main__":
    unittest.main()
