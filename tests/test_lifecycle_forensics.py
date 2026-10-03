"""暂停、恢复、撤销与时点取证。"""

import unittest
from datetime import datetime, timedelta, timezone

from helpers import (
    BASE,
    approve_to_issuance,
    build_service,
    create_and_submit,
    fleet_actor,
    reviewer_actor,
    standard_payload,
)
from permit_coordination.errors import ValidationError, WorkflowError


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.service, self.store, self.clock = build_service()
        create_and_submit(self.service)
        approve_to_issuance(self.service)
        self.issued_at = self.clock.now()
        self.clock.advance(days=1)

    def test_suspend_blocks_enforcement_and_resume_restores(self):
        suspend_at = self.clock.now()
        self.service.suspend("AP-1", "车辆出现安全告警，暂停路测", reviewer_actor())
        view = self.service.effective_view("AP-1", suspend_at + timedelta(minutes=1))
        self.assertEqual(view["state"], "suspended")
        # 许可仍存在（可追溯许可号），但授权全部不可执行
        self.assertIsNotNone(view["permit_number"])
        self.assertEqual(view["active_grants"], [])
        self.assertIsNotNone(view["suspension"])

        self.clock.advance(hours=6)
        self.service.resume("AP-1", "告警解除", reviewer_actor())
        after = self.service.effective_view("AP-1", self.clock.now())
        self.assertEqual(after["state"], "signed")
        self.assertIsNotNone(after["permit_number"])
        self.assertGreater(len(after["active_grants"]), 0)

    def test_suspend_requires_reason_and_signed_state(self):
        with self.assertRaises(ValidationError):
            self.service.suspend("AP-1", "   ", reviewer_actor())

    def test_resume_only_when_suspended(self):
        with self.assertRaises(WorkflowError):
            self.service.resume("AP-1", "x", reviewer_actor())

    def test_suspended_permit_cannot_accept_revision(self):
        self.service.suspend("AP-1", "安全告警调查中", reviewer_actor())
        with self.assertRaises(WorkflowError):
            self.service.submit_version("AP-1", standard_payload(note="暂停期修订"), fleet_actor())
        self.service.resume("AP-1", "调查结束", reviewer_actor())
        # 恢复后可以正常提交修订
        result = self.service.submit_version(
            "AP-1", standard_payload(note="恢复后修订"), fleet_actor()
        )
        self.assertEqual(result["version"], 2)

    def test_can_suspend_while_a_revision_is_under_review(self):
        # v2 提交后正在审查，现场出问题仍应能立即暂停现行 v1 许可
        self.service.submit_version("AP-1", standard_payload(note="v2 修订"), fleet_actor())
        result = self.service.suspend("AP-1", "现场事故，立即暂停", reviewer_actor())
        self.assertTrue(result["suspension_event_id"])
        view = self.service.get_application_view("AP-1")
        self.assertEqual(view["workflow_state"], "suspended")

    def test_revoke_invalidates_all_grants_and_state_is_final(self):
        revoked_at = self.clock.now()
        self.service.revoke("AP-1", "资质材料造假", reviewer_actor())
        view = self.service.effective_view("AP-1", revoked_at + timedelta(seconds=1))
        self.assertEqual(view["state"], "revoked")
        self.assertEqual(view["active_grants"], [])
        grants = self.service.get_application_view("AP-1")["grants"]
        self.assertTrue(all(g["status"] == "invalidated" for g in grants))
        self.assertTrue(all(g["invalidated"]["reason"] == "permit_revoked" for g in grants))
        with self.assertRaises(WorkflowError):
            self.service.submit_version("AP-1", standard_payload(note="试图复活"), fleet_actor())

    def test_as_of_before_revocation_shows_then_valid_permit(self):
        before = self.clock.now()
        self.clock.advance(hours=2)
        self.service.revoke("AP-1", "事后发现问题", reviewer_actor())
        historical = self.service.effective_view("AP-1", before)
        self.assertEqual(historical["state"], "signed")
        self.assertIsNotNone(historical["permit_number"])
        # 撤销依据不出现在历史视图，但当前视图可见
        current = self.service.get_application_view("AP-1")
        self.assertEqual(current["state"], "revoked")
        self.assertEqual(current["revocation"]["reason"], "事后发现问题")

    def test_as_of_before_issuance_shows_no_permit(self):
        # 新建第二份申请尚未签发
        create_and_submit(self.service, "AP-2")
        view = self.service.effective_view("AP-2", self.clock.now())
        self.assertIsNone(view["permit_number"])
        self.assertIn(view["state"], ("review", "info_requested", "draft"))

    def test_historical_view_names_responsible_issuer_and_basis(self):
        at = self.issued_at + timedelta(hours=1)
        view = self.service.effective_view("AP-1", at)
        self.assertEqual(view["responsible"]["person_id"], "P-1")
        self.assertEqual(view["issued_by"]["user_id"], "U-REV-1")
        self.assertEqual(view["rules_version"], "2026.09")
        grant = next(g for g in view["active_grants"] if g["kind"] == "vehicle")
        self.assertTrue(grant["basis"]["rule_check_event_id"].startswith("evt_"))
        self.assertEqual(len(grant["basis"]["countersign_event_ids"]), 2)
        self.assertEqual(grant["basis"]["permit_number"], view["permit_number"])

    def test_expired_route_window_is_not_enforceable_but_still_recorded(self):
        # 签发的时窗是 10-21 01:00-05:00；把检查点放到时窗结束后
        check_at = datetime(2026, 10, 21, 6, 0, tzinfo=timezone.utc)
        view = self.service.effective_view("AP-1", check_at)
        self.assertEqual(view["state"], "signed")
        self.assertEqual(len(view["active_route_windows"]), 1)  # 许可内仍登记
        self.assertEqual(view["enforceable_route_windows"], [])  # 现场不可再引用


if __name__ == "__main__":
    unittest.main()
