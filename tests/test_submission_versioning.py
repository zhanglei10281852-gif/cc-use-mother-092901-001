"""版本提交、内容去重与幂等。"""

import unittest

from helpers import (
    BASE,
    approve_to_issuance,
    build_service,
    create_and_submit,
    fleet_actor,
    standard_payload,
    vehicle,
)
from permit_coordination.errors import ConflictError, WorkflowError


class SubmissionVersioningTests(unittest.TestCase):
    def setUp(self):
        self.service, self.store, self.clock = build_service()

    def test_first_submission_creates_version_one(self):
        result = create_and_submit(self.service)
        self.assertEqual(result["version"], 1)
        self.assertFalse(result["deduplicated"])
        self.assertEqual(len(result["content_digest"]), 16)

    def test_identical_resubmission_is_rejected_without_new_version(self):
        create_and_submit(self.service)
        with self.assertRaises(ConflictError) as ctx:
            self.service.submit_version("AP-1", standard_payload(), fleet_actor())
        self.assertEqual(ctx.exception.details["version"], 1)
        view = self.service.get_application_view("AP-1")
        self.assertEqual(view["current_version"], 1)

    def test_idempotency_key_collapses_duplicate_submissions(self):
        first = create_and_submit(self.service, idempotency_key="fleet-ticket-77")
        second = self.service.submit_version(
            "AP-1", standard_payload(idempotency_key="fleet-ticket-77"), fleet_actor()
        )
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["version"], first["version"])
        self.assertEqual(len(self.store.load_stream("app:AP-1")), 2)  # 创建 + 一次提交

    def test_content_change_creates_new_version_and_keeps_snapshots_immutable(self):
        create_and_submit(self.service)
        v1 = self.service.get_application_view("AP-1")["versions"][0]
        payload = standard_payload(vehicles=[vehicle(qualification_version="v2")])
        result = self.service.submit_version("AP-1", payload, fleet_actor())
        self.assertEqual(result["version"], 2)
        versions = self.service.get_application_view("AP-1")["versions"]
        self.assertEqual(versions[0]["vehicles"][0]["qualification_version"], "v1")
        self.assertEqual(versions[1]["vehicles"][0]["qualification_version"], "v2")
        self.assertNotEqual(versions[0]["content_digest"], versions[1]["content_digest"])

    def test_revoked_application_cannot_accept_new_versions(self):
        create_and_submit(self.service)
        approve_to_issuance(self.service)
        self.service.revoke("AP-1", "资质造假", fleet_actor())
        with self.assertRaises(WorkflowError):
            self.service.submit_version(
                "AP-1",
                standard_payload(vehicles=[vehicle(qualification_version="v9")]),
                fleet_actor(),
            )


if __name__ == "__main__":
    unittest.main()
