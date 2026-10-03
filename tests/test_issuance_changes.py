"""签发后变更：显式失效、沿用、许可号不重复。"""

import unittest

from helpers import (
    approve_to_issuance,
    build_service,
    create_and_submit,
    driver,
    fleet_actor,
    reviewer_actor,
    safety_actor,
    police_actor,
    standard_payload,
    vehicle,
    window,
)
from permit_coordination.errors import WorkflowError
from permit_coordination.contracts import ApprovalKind
from datetime import timedelta
from helpers import BASE


class IssuanceChangeTests(unittest.TestCase):
    def setUp(self):
        self.service, self.store, self.clock = build_service()
        create_and_submit(self.service)
        approve_to_issuance(self.service)

    def _active_grant(self, kind: str):
        grants = [
            g for g in self.service.get_application_view("AP-1")["grants"]
            if g["kind"] == kind and g["status"] == "active"
        ]
        return grants

    def test_reissue_keeps_single_permit_number(self):
        first = self.service.get_application_view("AP-1")["permit_number"]
        payload = standard_payload(note="增加一辆车", vehicles=[vehicle(), vehicle(vehicle_id="V-2")])
        self.service.submit_version("AP-1", payload, fleet_actor())
        approve_to_issuance(self.service)
        view = self.service.get_application_view("AP-1")
        self.assertEqual(view["permit_number"], first)
        self.assertEqual(len(view["issues"]), 2)

    def test_vehicle_qualification_change_invalidates_old_grant_with_replacement(self):
        payload = standard_payload(vehicles=[vehicle(qualification_version="v2-new")])
        result = self.service.submit_version("AP-1", payload, fleet_actor())
        changed = [e for e in result["impact"] if e["effect"] == "changed"]
        self.assertEqual(len(changed), 1)
        self.assertTrue(changed[0]["replaced_by"].startswith("vehicle:V-1@"))
        approve_to_issuance(self.service)
        grants = self.service.get_application_view("AP-1")["grants"]
        invalid = [g for g in grants if g["status"] == "invalidated" and g["kind"] == "vehicle"]
        active_vehicles = [g for g in grants if g["status"] == "active" and g["kind"] == "vehicle"]
        self.assertEqual(len(invalid), 1)
        self.assertEqual(invalid[0]["invalidated"]["reason"], "item_changed")
        self.assertEqual(invalid[0]["invalidated"]["replaced_by"], active_vehicles[0]["item_id"])
        self.assertEqual(len(active_vehicles), 1)

    def test_removed_vehicle_grant_invalidated_as_removed(self):
        payload = standard_payload(
            vehicles=[vehicle(vehicle_id="V-2")],  # 完全删除 V-1，换 V-2
        )
        self.service.submit_version("AP-1", payload, fleet_actor())
        approve_to_issuance(self.service)
        grants = self.service.get_application_view("AP-1")["grants"]
        invalid = [
            g for g in grants
            if g["status"] == "invalidated" and g["kind"] == "vehicle"
        ]
        self.assertEqual(invalid[0]["invalidated"]["reason"], "removed_in_new_version")
        self.assertIsNone(invalid[0]["invalidated"]["replaced_by"])

    def test_unchanged_items_are_carried_not_regranted(self):
        payload = standard_payload(
            route_windows=[window(start=BASE + timedelta(days=2), hours=2.0)]
        )
        self.service.submit_version("AP-1", payload, fleet_actor())
        result = approve_to_issuance(self.service)
        self.assertEqual(result["carried_count"], 3)  # 车/驾驶员/能力沿用
        self.assertEqual(result["new_grant_count"], 1)  # 仅新时窗
        self.assertEqual(result["invalidated_count"], 1)  # 旧时窗失效

    def test_new_version_resets_countersignatures(self):
        view0 = self.service.get_application_view("AP-1")
        self.assertEqual(len(view0["countersignatures"]), 2)
        payload = standard_payload(vehicles=[vehicle(qualification_version="v3")])
        self.service.submit_version("AP-1", payload, fleet_actor())
        # 新版本需要重新规则校验；旧会签仍在事件历史里，但本版本签发时必须重新会签
        self.service.record_rule_check("AP-1", reviewer_actor())
        with self.assertRaises(WorkflowError) as ctx:
            self.service.issue_permit("AP-1", reviewer_actor())
        self.assertEqual(
            set(ctx.exception.details["missing_roles"]),
            {"safety_officer", "traffic_police"},
        )
        approve_to_issuance(self.service)
        view = self.service.get_application_view("AP-1")
        # 会签视图只展示当前有效（最新版本）会签
        self.assertEqual(len(view["countersignatures"]), 2)
        self.assertTrue(all(c["version"] == 2 for c in view["countersignatures"]))

    def test_route_window_change_keeps_other_windows_active(self):
        payload = standard_payload(
            route_windows=[
                window(segment="R-S1", start=BASE + timedelta(days=1), hours=2.0),
                window(segment="R-S2", start=BASE + timedelta(days=3), hours=2.0),
            ]
        )
        self.service.submit_version("AP-1", payload, fleet_actor())
        approve_to_issuance(self.service)
        # 只改 R-S1 的时间，R-S2 沿用
        payload2 = standard_payload(
            route_windows=[
                window(segment="R-S1", start=BASE + timedelta(days=1, hours=6), hours=2.0),
                window(segment="R-S2", start=BASE + timedelta(days=3), hours=2.0),
            ]
        )
        self.service.submit_version("AP-1", payload2, fleet_actor())
        approve_to_issuance(self.service)
        effective = self.service.effective_view("AP-1", self.clock.now())
        segments = sorted(w["segment_code"] for w in effective["active_route_windows"])
        self.assertEqual(segments, ["R-S1", "R-S2"])


if __name__ == "__main__":
    unittest.main()
