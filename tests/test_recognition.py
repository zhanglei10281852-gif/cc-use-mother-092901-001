"""跨区域互认：认可范围子集、随源许可变更而收窄、撤回。"""

import unittest
from datetime import timedelta

from helpers import (
    approve_to_issuance,
    build_service,
    create_and_submit,
    fleet_actor,
    police_actor,
    reviewer_actor,
    safety_actor,
    standard_payload,
    vehicle,
)
from permit_coordination.errors import ValidationError, WorkflowError


def sz_reviewer():
    from permit_coordination.event_store import Actor

    return Actor("U-SZ-REV", "reviewer", "SZ_DEMO", "深圳审查员")


def sz_safety():
    from permit_coordination.event_store import Actor

    return Actor("U-SZ-SAFE", "safety_officer", "SZ_DEMO", "深圳安全员")


def sz_police():
    from permit_coordination.event_store import Actor

    return Actor("U-SZ-POL", "traffic_police", "SZ_DEMO", "深圳交警")


class RecognitionTests(unittest.TestCase):
    def setUp(self):
        self.service, self.store, self.clock = build_service()
        # 上海签发两辆车，便于验证子集认可
        create_and_submit(
            self.service,
            "AP-SH",
            vehicles=[vehicle(vehicle_id="V-1"), vehicle(vehicle_id="V-2")],
        )
        approve_to_issuance(self.service, "AP-SH")
        self.source_view = self.service.get_application_view("AP-SH")

    def test_recognize_full_scope_keeps_origin_basis(self):
        result = self.service.grant_recognition(
            {"source_application_id": "AP-SH", "recognizing_jurisdiction": "SZ_DEMO"},
            sz_reviewer(),
        )
        rec = self.service.get_recognition_view(result["recognition_id"])
        self.assertEqual(rec["state"], "recognized")
        self.assertEqual(rec["origin_jurisdiction"], "SH_DEMO")
        self.assertEqual(rec["recognizing_jurisdiction"], "SZ_DEMO")
        self.assertEqual(
            len(rec["scope"]["vehicle"]), 2
        )
        # 认可依据指向源签发事件、源规则版本
        self.assertEqual(rec["basis"]["source_rules_version"], "2026.09")
        self.assertTrue(rec["basis"]["source_issue_event_id"].startswith("evt_"))

    def test_recognized_scope_must_be_subset_of_source(self):
        with self.assertRaises(ValidationError) as ctx:
            self.service.grant_recognition(
                {
                    "source_application_id": "AP-SH",
                    "recognizing_jurisdiction": "SZ_DEMO",
                    "scope": {"vehicle_ids": ["vehicle:V-99@deadbeef"]},
                },
                sz_reviewer(),
            )
        self.assertIn("未生效", ctx.exception.message)

    def test_partial_recognition_only_effective_for_declared_scope(self):
        only_v1 = next(
            g["item_id"]
            for g in self.source_view["grants"]
            if g["kind"] == "vehicle" and g["item_id"].startswith("vehicle:V-1@")
        )
        result = self.service.grant_recognition(
            {
                "source_application_id": "AP-SH",
                "recognizing_jurisdiction": "SZ_DEMO",
                "scope": {"vehicle_ids": [only_v1]},
            },
            sz_reviewer(),
        )
        rec = self.service.get_recognition_view(result["recognition_id"])
        effective = rec["effective_scope_at"]["vehicle"]
        self.assertEqual(effective["recognized_and_effective"], [only_v1])
        self.assertEqual(effective["recognized_but_no_longer_effective"], [])
        self.assertEqual(rec["scope"]["vehicle"], [only_v1])

    def test_source_change_that_invalidates_grant_shrinks_effective_recognition(self):
        result = self.service.grant_recognition(
            {"source_application_id": "AP-SH", "recognizing_jurisdiction": "SZ_DEMO"},
            sz_reviewer(),
        )
        rec_id = result["recognition_id"]
        # 源许可换发：V-1 资质升级，旧 grant 失效
        self.service.submit_version(
            "AP-SH",
            standard_payload(vehicles=[vehicle(vehicle_id="V-1", qualification_version="v2"), vehicle(vehicle_id="V-2")]),
            fleet_actor(),
        )
        self.service.record_rule_check("AP-SH", reviewer_actor())
        self.service.countersign("AP-SH", "ok", safety_actor())
        self.service.countersign("AP-SH", "ok", police_actor())
        self.service.issue_permit("AP-SH", reviewer_actor())

        rec = self.service.get_recognition_view(rec_id)
        old_v1 = next(
            i for i in rec["scope"]["vehicle"] if i.startswith("vehicle:V-1@")
        )
        # 认可记录本身保留历史范围（各方实际认可过什么），但生效范围自动收窄
        self.assertIn(
            old_v1, rec["effective_scope_at"]["vehicle"]["recognized_but_no_longer_effective"]
        )
        live = rec["effective_scope_at"]["vehicle"]["recognized_and_effective"]
        self.assertTrue(any(i.startswith("vehicle:V-2@") for i in live))

    def test_scope_amendment_can_only_narrow(self):
        result = self.service.grant_recognition(
            {"source_application_id": "AP-SH", "recognizing_jurisdiction": "SZ_DEMO"},
            sz_reviewer(),
        )
        rec_id = result["recognition_id"]
        vehicles_before = list(
            self.service.get_recognition_view(rec_id)["scope"]["vehicle"]
        )
        self.service.amend_recognition_scope(
            rec_id,
            {"vehicle_ids": [], "driver_ids": [], "capability_codes": [], "window_ids": []},
            "收窄为暂不认可任何条目",
            sz_reviewer(),
        )
        rec = self.service.get_recognition_view(rec_id)
        self.assertEqual(rec["scope"]["vehicle"], [])
        # 试图把先前条目加回来属于扩大，必须拒绝
        with self.assertRaises(WorkflowError):
            self.service.amend_recognition_scope(
                rec_id,
                {"vehicle_ids": vehicles_before[:1], "driver_ids": [], "capability_codes": [], "window_ids": []},
                "想加回来",
                sz_reviewer(),
            )

    def test_withdraw_recognition(self):
        result = self.service.grant_recognition(
            {"source_application_id": "AP-SH", "recognizing_jurisdiction": "SZ_DEMO"},
            sz_reviewer(),
        )
        rec_id = result["recognition_id"]
        self.service.withdraw_recognition(rec_id, "发现源辖区规则版本存在缺陷", sz_reviewer())
        rec = self.service.get_recognition_view(rec_id)
        self.assertEqual(rec["state"], "withdrawn")
        self.assertEqual(rec["withdrawal"]["reason"], "发现源辖区规则版本存在缺陷")
        with self.assertRaises(WorkflowError):
            self.service.amend_recognition_scope(rec_id, {}, "撤回后修改", sz_reviewer())

    def test_cannot_recognize_non_effective_permit(self):
        create_and_submit(self.service, "AP-DRAFT")
        with self.assertRaises(WorkflowError):
            self.service.grant_recognition(
                {"source_application_id": "AP-DRAFT", "recognizing_jurisdiction": "SZ_DEMO"},
                sz_reviewer(),
            )

    def test_suspended_source_freezes_effective_recognition(self):
        result = self.service.grant_recognition(
            {"source_application_id": "AP-SH", "recognizing_jurisdiction": "SZ_DEMO"},
            sz_reviewer(),
        )
        rec_id = result["recognition_id"]
        self.service.suspend("AP-SH", "源辖区安全调查", reviewer_actor())
        rec = self.service.get_recognition_view(rec_id)
        self.assertEqual(rec["source_permit_state_at"], "suspended")
        self.assertEqual(
            rec["effective_scope_at"]["vehicle"]["recognized_and_effective"], []
        )


if __name__ == "__main__":
    unittest.main()
