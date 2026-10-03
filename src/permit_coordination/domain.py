"""事件投影：把只追加事件折叠为当前状态，也可折叠到任意历史时点。"""

from dataclasses import dataclass, field
from datetime import datetime

from permit_coordination.contracts import RouteWindow
from permit_coordination.events import (
    ApplicationSubmitted,
    ConflictDetected,
    ConflictResolved,
    Event,
    PermitIssued,
    PermitReinstated,
    PermitRevoked,
    PermitSuperseded,
    PermitSuspended,
    RecognitionRecorded,
    RecognitionRevoked,
    ReviewApproved,
    ReviewOpened,
    ReviewRejected,
    RoadClosureRegistered,
    SupplementRequested,
    SupplementSubmitted,
)


def windows_overlap(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    return start_a < end_b and start_b < end_a


@dataclass
class FindingView:
    code: str
    severity: str  # blocking | warning
    message: str
    resolved: bool = False
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    evidence: tuple[str, ...] = ()


@dataclass
class ReviewView:
    review_id: str
    application_id: str
    version: int
    jurisdiction: str
    opened_at: datetime
    opened_by: str
    state: str = "review"  # review | supplement_requested | approved | rejected
    findings: list[FindingView] = field(default_factory=list)
    requested_items: tuple[str, ...] = ()
    documents: list[dict] = field(default_factory=list)
    conditions: tuple[str, ...] = ()
    reason: str = ""
    decided_at: datetime | None = None
    decided_by: str | None = None


@dataclass
class SubmissionView:
    submission_id: str
    application_id: str
    version: int
    fleet_id: str
    jurisdiction: str
    at: datetime
    actor: str
    vehicle_specs: dict
    driver_authorizations: dict
    capability: dict
    route_windows: tuple[RouteWindow, ...]
    content_hash: str
    supersedes_submission_id: str | None
    superseded_permit_ids: tuple[str, ...]
    changed_vehicles: tuple[str, ...]
    changed_segments: tuple[str, ...]
    duplicate_of: str | None = None
    superseded_by: str | None = None
    idempotency_key: str | None = None


@dataclass
class StatusChange:
    at: datetime
    actor: str
    from_status: str
    to_status: str
    reason: str = ""


@dataclass
class PermitView:
    permit_id: str
    application_id: str
    version: int
    fleet_id: str
    issuing_jurisdiction: str
    approving_jurisdictions: tuple[str, ...]
    vehicle_ids: tuple[str, ...]
    driver_ids: tuple[str, ...]
    route_windows: tuple[RouteWindow, ...]
    review_basis: tuple[dict, ...]
    validity_start: datetime
    validity_end: datetime
    issued_at: datetime
    issued_by: str
    status: str = "issued"  # issued | suspended | revoked | superseded
    changes: list[StatusChange] = field(default_factory=list)
    replaced_by_permit_id: str | None = None
    supersede_reason: str = ""
    changed_vehicles: tuple[str, ...] = ()
    changed_segments: tuple[str, ...] = ()
    # 封路冲突改期/取消：原时窗键 -> 新时窗
    rescheduled: dict[tuple, RouteWindow] = field(default_factory=dict)
    cancelled_windows: set[tuple] = field(default_factory=set)

    @staticmethod
    def window_key(window: RouteWindow) -> tuple:
        return (window.segment_code, window.starts_at, window.ends_at)

    def effective_windows(self) -> tuple[RouteWindow, ...]:
        result = []
        for window in self.route_windows:
            key = self.window_key(window)
            if key in self.cancelled_windows:
                continue
            result.append(self.rescheduled.get(key, window))
        return tuple(result)

    @property
    def effective_validity_start(self) -> datetime:
        windows = self.effective_windows()
        return min((w.starts_at for w in windows), default=self.validity_start)

    @property
    def effective_validity_end(self) -> datetime:
        windows = self.effective_windows()
        return max((w.ends_at for w in windows), default=self.validity_end)

    def status_at(self, at: datetime) -> str:
        status = self.status if self.changes else "issued"
        # changes 已按时间顺序记录，逐步推演到 at
        current = "issued"
        for change in self.changes:
            if change.at <= at:
                current = change.to_status
            else:
                break
        return current


@dataclass
class RecognitionView:
    recognition_id: str
    permit_id: str
    recognizing_jurisdiction: str
    vehicle_ids: tuple[str, ...]
    driver_ids: tuple[str, ...]
    segment_codes: tuple[str, ...]
    window_start: datetime
    window_end: datetime
    basis: str
    recorded_at: datetime
    recorded_by: str
    state: str = "active"  # active | revoked | superseded
    revoked_reason: str = ""
    revoked_at: datetime | None = None
    revoked_by: str | None = None
    superseded_by: str | None = None


@dataclass
class ClosureView:
    closure_id: str
    segment_codes: tuple[str, ...]
    starts_at: datetime
    ends_at: datetime
    reason: str
    registered_at: datetime
    registered_by: str


@dataclass
class ConflictView:
    conflict_id: str
    closure_id: str
    permit_id: str
    segment_code: str
    window_start: datetime
    window_end: datetime
    closure_start: datetime
    closure_end: datetime
    detected_at: datetime
    detected_by: str
    state: str = "open"  # open | rescheduled | cancelled
    new_window: RouteWindow | None = None
    resolution_note: str = ""
    resolved_at: datetime | None = None
    resolved_by: str | None = None


class Projection:
    def __init__(self) -> None:
        self.submissions: dict[str, SubmissionView] = {}
        self.application_versions: dict[str, list[str]] = {}
        self.reviews: dict[str, ReviewView] = {}
        self.reviews_by_version: dict[tuple[str, int], dict[str, str]] = {}
        self.permits: dict[str, PermitView] = {}
        self.permit_ids_by_application: dict[str, list[str]] = {}
        self.recognitions: dict[str, RecognitionView] = {}
        self.recognition_by_pair: dict[tuple[str, str], str] = {}
        self.closures: dict[str, ClosureView] = {}
        self.conflicts: dict[str, ConflictView] = {}
        self.conflicts_by_permit: dict[str, list[str]] = {}
        self.event_count_by_type: dict[str, int] = {}

    def apply(self, event: Event) -> None:
        self.event_count_by_type[type(event).__name__] = self.event_count_by_type.get(type(event).__name__, 0) + 1
        handler = getattr(self, f"_on_{type(event).__name__}", None)
        if handler:
            handler(event)

    def _on_ApplicationSubmitted(self, e: ApplicationSubmitted) -> None:
        view = SubmissionView(
            submission_id=e.submission_id, application_id=e.application_id, version=e.version,
            fleet_id=e.fleet_id, jurisdiction=e.jurisdiction, at=e.at, actor=e.actor,
            vehicle_specs=dict(e.vehicle_specs), driver_authorizations=dict(e.driver_authorizations),
            capability=dict(e.capability), route_windows=e.route_windows,
            content_hash=e.submission_id and _hash_content(
                e.vehicle_specs, e.driver_authorizations, e.capability, e.route_windows),
            supersedes_submission_id=e.supersedes_submission_id,
            superseded_permit_ids=e.superseded_permit_ids,
            changed_vehicles=e.changed_vehicles, changed_segments=e.changed_segments,
            duplicate_of=e.duplicate_of_submission_id,
            idempotency_key=e.idempotency_key,
        )
        self.submissions[e.submission_id] = view
        self.application_versions.setdefault(e.application_id, []).append(e.submission_id)
        if e.supersedes_submission_id and not e.duplicate_of_submission_id:
            prior = self.submissions.get(e.supersedes_submission_id)
            if prior:
                prior.superseded_by = e.submission_id

    def _on_ReviewOpened(self, e: ReviewOpened) -> None:
        view = ReviewView(
            review_id=e.review_id, application_id=e.application_id, version=e.version,
            jurisdiction=e.jurisdiction, opened_at=e.at, opened_by=e.actor,
        )
        self.reviews[e.review_id] = view
        self.reviews_by_version.setdefault((e.application_id, e.version), {})[e.jurisdiction] = e.review_id

    def _on_SupplementRequested(self, e: SupplementRequested) -> None:
        review = self.reviews[e.review_id]
        review.state = "supplement_requested"
        review.requested_items = e.requested_items
        existing_codes = {f.code for f in review.findings}
        for item in e.findings:
            if item["code"] not in existing_codes:
                review.findings.append(FindingView(
                    code=item["code"], severity=item.get("severity", "blocking"),
                    message=item.get("message", "")))
                existing_codes.add(item["code"])

    def _on_SupplementSubmitted(self, e: SupplementSubmitted) -> None:
        review = self.reviews[e.review_id]
        for document in e.documents:
            review.documents.append(document)
        resolved = set(e.resolved_finding_codes)
        for finding in review.findings:
            if finding.code in resolved:
                finding.resolved = True
                finding.resolved_at = e.at
                finding.resolved_by = e.actor
                finding.evidence = tuple(
                    d.get("document_id", "") for d in e.documents if d.get("document_id"))
        if all(f.resolved or f.severity != "blocking" for f in review.findings):
            review.state = "review"

    def _on_ReviewApproved(self, e: ReviewApproved) -> None:
        review = self.reviews[e.review_id]
        review.state = "approved"
        review.conditions = e.conditions
        review.decided_at = e.at
        review.decided_by = e.actor

    def _on_ReviewRejected(self, e: ReviewRejected) -> None:
        review = self.reviews[e.review_id]
        review.state = "rejected"
        review.reason = e.reason
        review.decided_at = e.at
        review.decided_by = e.actor

    def _on_PermitIssued(self, e: PermitIssued) -> None:
        view = PermitView(
            permit_id=e.permit_id, application_id=e.application_id, version=e.version,
            fleet_id=e.fleet_id, issuing_jurisdiction=e.issuing_jurisdiction,
            approving_jurisdictions=e.approving_jurisdictions, vehicle_ids=e.vehicle_ids,
            driver_ids=e.driver_ids, route_windows=e.route_windows, review_basis=e.review_basis,
            validity_start=e.validity_start, validity_end=e.validity_end,
            issued_at=e.at, issued_by=e.actor,
        )
        view.changes.append(StatusChange(at=e.at, actor=e.actor, from_status="", to_status="issued"))
        self.permits[e.permit_id] = view
        self.permit_ids_by_application.setdefault(e.application_id, []).append(e.permit_id)

    def _on_PermitSuspended(self, e: PermitSuspended) -> None:
        permit = self.permits[e.permit_id]
        permit.changes.append(StatusChange(
            at=e.at, actor=e.actor, from_status=permit.status, to_status="suspended", reason=e.reason))
        permit.status = "suspended"

    def _on_PermitReinstated(self, e: PermitReinstated) -> None:
        permit = self.permits[e.permit_id]
        permit.changes.append(StatusChange(
            at=e.at, actor=e.actor, from_status=permit.status, to_status="issued"))
        permit.status = "issued"

    def _on_PermitRevoked(self, e: PermitRevoked) -> None:
        permit = self.permits[e.permit_id]
        permit.changes.append(StatusChange(
            at=e.at, actor=e.actor, from_status=permit.status, to_status="revoked", reason=e.reason))
        permit.status = "revoked"

    def _on_PermitSuperseded(self, e: PermitSuperseded) -> None:
        permit = self.permits[e.permit_id]
        permit.changes.append(StatusChange(
            at=e.at, actor=e.actor, from_status=permit.status, to_status="superseded",
            reason=e.reason))
        permit.status = "superseded"
        permit.replaced_by_permit_id = e.replaced_by_permit_id
        permit.supersede_reason = e.reason
        permit.changed_vehicles = e.changed_vehicles
        permit.changed_segments = e.changed_segments

    def _on_RecognitionRecorded(self, e: RecognitionRecorded) -> None:
        pair = (e.permit_id, e.recognizing_jurisdiction)
        previous_id = self.recognition_by_pair.get(pair)
        if previous_id:
            previous = self.recognitions[previous_id]
            if previous.state == "active":
                previous.state = "superseded"
                previous.superseded_by = e.recognition_id
        view = RecognitionView(
            recognition_id=e.recognition_id, permit_id=e.permit_id,
            recognizing_jurisdiction=e.recognizing_jurisdiction, vehicle_ids=e.vehicle_ids,
            driver_ids=e.driver_ids, segment_codes=e.segment_codes,
            window_start=e.window_start, window_end=e.window_end, basis=e.basis,
            recorded_at=e.at, recorded_by=e.actor,
        )
        self.recognitions[e.recognition_id] = view
        self.recognition_by_pair[pair] = e.recognition_id

    def _on_RecognitionRevoked(self, e: RecognitionRevoked) -> None:
        view = self.recognitions[e.recognition_id]
        view.state = "revoked"
        view.revoked_reason = e.reason
        view.revoked_at = e.at
        view.revoked_by = e.actor

    def _on_RoadClosureRegistered(self, e: RoadClosureRegistered) -> None:
        self.closures[e.closure_id] = ClosureView(
            closure_id=e.closure_id, segment_codes=e.segment_codes, starts_at=e.starts_at,
            ends_at=e.ends_at, reason=e.reason, registered_at=e.at, registered_by=e.actor)

    def _on_ConflictDetected(self, e: ConflictDetected) -> None:
        view = ConflictView(
            conflict_id=e.conflict_id, closure_id=e.closure_id, permit_id=e.permit_id,
            segment_code=e.segment_code, window_start=e.window_start, window_end=e.window_end,
            closure_start=e.closure_start, closure_end=e.closure_end,
            detected_at=e.at, detected_by=e.actor)
        self.conflicts[e.conflict_id] = view
        self.conflicts_by_permit.setdefault(e.permit_id, []).append(e.conflict_id)

    def _on_ConflictResolved(self, e: ConflictResolved) -> None:
        conflict = self.conflicts[e.conflict_id]
        conflict.state = e.resolution
        conflict.new_window = e.new_window
        conflict.resolution_note = e.note
        conflict.resolved_at = e.at
        conflict.resolved_by = e.actor
        permit = self.permits[conflict.permit_id]
        key = (conflict.segment_code, conflict.window_start, conflict.window_end)
        if e.resolution == "rescheduled" and e.new_window is not None:
            permit.rescheduled[key] = e.new_window
            permit.cancelled_windows.discard(key)
        elif e.resolution == "cancelled":
            permit.cancelled_windows.add(key)
            permit.rescheduled.pop(key, None)

    # ---- 查询辅助 ----

    def latest_submission(self, application_id: str) -> SubmissionView | None:
        ids = self.application_versions.get(application_id)
        if not ids:
            return None
        return self.submissions[ids[-1]]

    def active_submissions(self, application_id: str) -> list[SubmissionView]:
        return [self.submissions[s] for s in self.application_versions.get(application_id, [])
                if self.submissions[s].superseded_by is None
                and self.submissions[s].duplicate_of is None]

    def effective_permits(self, at: datetime) -> list[PermitView]:
        """在 at 时点处于有效（已签发、未暂停/撤销/取代）且覆盖该时点的许可。"""
        result = []
        for permit in self.permits.values():
            if permit.issued_at > at:
                continue
            if permit.status_at(at) != "issued":
                continue
            if not (permit.effective_validity_start <= at < permit.effective_validity_end):
                continue
            result.append(permit)
        return result

    @classmethod
    def replay(cls, events, upto: datetime | None = None) -> "Projection":
        projection = cls()
        for event in events:
            if upto is not None and event.at > upto:
                break
            projection.apply(event)
        return projection


def _hash_content(vehicle_specs: dict, driver_authorizations: dict, capability: dict,
                  route_windows: tuple[RouteWindow, ...]) -> str:
    import hashlib
    import json

    payload = {
        "vehicles": vehicle_specs,
        "drivers": driver_authorizations,
        "capability": capability,
        "windows": [
            {"segment": w.segment_code, "start": w.starts_at.isoformat(), "end": w.ends_at.isoformat()}
            for w in route_windows
        ],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def content_hash(vehicle_specs: dict, driver_authorizations: dict, capability: dict,
                 route_windows: tuple[RouteWindow, ...]) -> str:
    return _hash_content(vehicle_specs, driver_authorizations, capability, route_windows)
