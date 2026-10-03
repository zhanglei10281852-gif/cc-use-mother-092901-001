"""事件回放得到的领域聚合。

聚合对象本身不做决策，只负责把事件流折叠成当前（或某一时点的）状态；
所有状态变更都由 :mod:`permit_coordination.service` 以追加事件的方式完成。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .contracts import (
    ApprovalKind,
    ApplicationVersion,
    ApprovalStatus,
    ConflictStatus,
    DriverAuthorization,
    PermitState,
    RecognitionState,
    RouteWindow,
    TestCapability,
    VehicleQualification,
)
from .event_store import Event
from .serialization import (
    capability_to_dict,
    driver_to_dict,
    parse_dt,
    vehicle_to_dict,
    window_to_dict,
)


# ---------------------------------------------------------------------------
# 快照编解码（随事件保存，保证旧版本永不被后续修改污染）
# ---------------------------------------------------------------------------


def version_to_data(snapshot: ApplicationVersion) -> dict[str, Any]:
    return {
        "version": snapshot.version,
        "note": snapshot.note,
        "submitted_at": snapshot.submitted_at.isoformat(),
        "content_digest": snapshot.content_digest,
        "vehicles": [vehicle_to_dict(v) for v in snapshot.vehicles],
        "drivers": [driver_to_dict(d) for d in snapshot.drivers],
        "capabilities": [capability_to_dict(c) for c in snapshot.capabilities],
        "route_windows": [window_to_dict(w) for w in snapshot.route_windows],
    }


def version_from_data(data: dict[str, Any]) -> ApplicationVersion:
    from .serialization import (
        capability_from_dict,
        driver_from_dict,
        vehicle_from_dict,
        window_from_dict,
    )

    return ApplicationVersion(
        version=data["version"],
        vehicles=tuple(vehicle_from_dict(v) for v in data["vehicles"]),
        drivers=tuple(driver_from_dict(d) for d in data["drivers"]),
        capabilities=tuple(capability_from_dict(c) for c in data["capabilities"]),
        route_windows=tuple(window_from_dict(w) for w in data["route_windows"]),
        note=data.get("note", ""),
        content_digest=data["content_digest"],
        submitted_at=parse_dt(data["submitted_at"], "submitted_at"),
    )


# ---------------------------------------------------------------------------
# 批准项（grant）：许可的最小效力单元，携带完整审批依据
# ---------------------------------------------------------------------------


@dataclass
class Grant:
    grant_id: str
    kind: ApprovalKind
    item_id: str
    introduced_version: int
    issued_at: datetime
    status: ApprovalStatus = ApprovalStatus.ACTIVE
    basis: dict[str, Any] = field(default_factory=dict)
    invalidated: dict[str, Any] | None = None
    carried_versions: list[int] = field(default_factory=list)

    def is_active_at(self, at: datetime) -> bool:
        """时点敏感：当前已失效的授权在失效之前仍然有效。"""
        if self.issued_at > at:
            return False
        if self.invalidated and self.invalidated["at"] <= at:
            return False
        return True

    def is_currently_active(self) -> bool:
        return self.status == ApprovalStatus.ACTIVE


@dataclass
class IssueRecord:
    version: int
    permit_number: str
    at: datetime
    event_id: str
    actor: dict[str, Any]
    rules_version: str


@dataclass
class SuspensionRecord:
    suspended_at: datetime
    reason: str
    actor: dict[str, Any]
    resumed_at: datetime | None = None
    resume_actor: dict[str, Any] | None = None


@dataclass
class Countersignature:
    role: str
    at: datetime
    actor: dict[str, Any]
    comment: str
    version: int
    event_id: str


@dataclass
class RuleCheck:
    version: int
    at: datetime
    actor: dict[str, Any]
    rules_version: str
    findings: list[dict[str, Any]]
    event_id: str

    def blockers(self) -> list[dict[str, Any]]:
        return [f for f in self.findings if f["severity"] == "blocker"]


@dataclass
class ImpactEntry:
    """版本变更对既有批准的影响分析。"""

    grant_id: str
    kind: str
    item_id: str
    effect: str  # carried | changed | removed | added
    reason: str
    replaced_by: str | None = None


@dataclass
class PermitAggregate:
    application_id: str
    fleet_id: str = ""
    jurisdiction: str = ""
    responsible: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    state: PermitState = PermitState.DRAFT
    versions: dict[int, ApplicationVersion] = field(default_factory=dict)
    current_version: int = 0
    version_state: dict[int, str] = field(default_factory=dict)
    grants: dict[str, Grant] = field(default_factory=dict)
    countersignatures: dict[str, Countersignature] = field(default_factory=dict)
    rule_checks: list[RuleCheck] = field(default_factory=list)
    issues: list[IssueRecord] = field(default_factory=list)
    suspensions: list[SuspensionRecord] = field(default_factory=list)
    revocation: dict[str, Any] | None = None
    last_impact: list[ImpactEntry] = field(default_factory=list)
    idempotency: dict[str, int] = field(default_factory=dict)
    info_requests: list[dict[str, Any]] = field(default_factory=list)
    amendments: list[dict[str, Any]] = field(default_factory=list)
    state_transitions: list[tuple[datetime, PermitState]] = field(default_factory=list)

    # ---- 便捷查询 -----------------------------------------------------

    def snapshot(self, version: int | None = None) -> ApplicationVersion:
        version = version or self.current_version
        return self.versions[version]

    def latest_rule_check(self, version: int) -> RuleCheck | None:
        for check in reversed(self.rule_checks):
            if check.version == version:
                return check
        return None

    def effective_issue_at(self, at: datetime) -> IssueRecord | None:
        """时点 ``at`` 实际生效的签发记录（可能不是当前版本）。"""
        record: IssueRecord | None = None
        for issue in self.issues:
            if issue.at <= at:
                record = issue
        return record

    def suspended_at(self, at: datetime) -> SuspensionRecord | None:
        for record in self.suspensions:
            if record.suspended_at <= at and (
                record.resumed_at is None or at < record.resumed_at
            ):
                return record
        return None

    def effective_state_at(self, at: datetime) -> PermitState:
        """按时点回放状态迁移：撤销前的检查时点仍然显示当时状态。"""
        transitions = [(t, s) for t, s in self.state_transitions if t <= at]
        if transitions:
            return transitions[-1][1]
        return PermitState.DRAFT

    def active_grants_at(self, at: datetime, kind: ApprovalKind | None = None) -> list[Grant]:
        issue = self.effective_issue_at(at)
        if issue is None:
            return []
        result = []
        for grant in self.grants.values():
            if kind is not None and grant.kind != kind:
                continue
            if grant.introduced_version > issue.version:
                continue
            if grant.is_active_at(at):
                result.append(grant)
        return result

    def active_windows_at(self, at: datetime) -> list[RouteWindow]:
        issue = self.effective_issue_at(at)
        if issue is None:
            return []
        snapshot = self.versions[issue.version]
        windows = {w.window_id: w for w in snapshot.route_windows}
        # 修正（改期）会在同一签发版本上替换时窗
        for amendment in self.amendments:
            from .serialization import parse_dt as _parse_dt

            if _parse_dt(amendment["at"], "amendment.at") > at:
                continue
            for removed in amendment["removed_window_ids"]:
                windows.pop(removed, None)
            for raw in amendment["added_windows"]:
                if raw["window_id"] not in windows:
                    from .serialization import window_from_dict

                    win = window_from_dict(raw)
                    windows[win.window_id] = win
        active_ids = {
            g.item_id.rsplit(":", 1)[-1]
            for g in self.active_grants_at(at, ApprovalKind.ROUTE_WINDOW)
        }
        return [w for wid, w in windows.items() if wid in active_ids]


# ---------------------------------------------------------------------------
# 折叠器
# ---------------------------------------------------------------------------


def apply_event(agg: PermitAggregate, event: Event) -> None:
    data = event.data
    t = event.event_type

    if t == "ApplicationCreated":
        agg.fleet_id = data["fleet_id"]
        agg.jurisdiction = data["jurisdiction"]
        agg.responsible = data["responsible"]
        agg.created_at = event.at
        agg.state_transitions.append((event.at, PermitState.DRAFT))

    elif t == "VersionSubmitted":
        snap = version_from_data(data["snapshot"])
        # 上一版尚未签发就被新提交取代
        if agg.current_version and agg.version_state.get(agg.current_version) == "submitted":
            agg.version_state[agg.current_version] = "superseded"
        agg.versions[snap.version] = snap
        agg.current_version = snap.version
        agg.version_state[snap.version] = "submitted"
        agg.state = PermitState.REVIEW
        if data.get("idempotency_key"):
            agg.idempotency[data["idempotency_key"]] = snap.version
        if data.get("impact"):
            agg.last_impact = [_impact_from_dict(e) for e in data["impact"]]
        # 已签发后的修订审查不影响既有许可的对外效力
        if not agg.issues:
            agg.state_transitions.append((event.at, PermitState.REVIEW))

    elif t == "InfoRequested":
        agg.state = PermitState.INFO_REQUESTED
        agg.info_requests.append(
            {
                "version": data["version"],
                "at": event.at.isoformat(),
                "actor": event.actor,
                "questions": data["questions"],
                "event_id": event.event_id,
            }
        )
        if not agg.issues:
            agg.state_transitions.append((event.at, PermitState.INFO_REQUESTED))

    elif t == "RuleCheckRecorded":
        check = RuleCheck(
            version=data["version"],
            at=event.at,
            actor=event.actor,
            rules_version=data["rules_version"],
            findings=data["findings"],
            event_id=event.event_id,
        )
        agg.rule_checks.append(check)

    elif t == "Countersigned":
        sig = Countersignature(
            role=data["role"],
            at=event.at,
            actor=event.actor,
            comment=data.get("comment", ""),
            version=data["version"],
            event_id=event.event_id,
        )
        agg.countersignatures[data["role"]] = sig

    elif t == "CountersignaturesReset":
        for role in data["roles"]:
            agg.countersignatures.pop(role, None)

    elif t == "PermitIssued":
        agg.state = PermitState.SIGNED
        issue = IssueRecord(
            version=data["version"],
            permit_number=data["permit_number"],
            at=event.at,
            event_id=event.event_id,
            actor=event.actor,
            rules_version=data["rules_version"],
        )
        agg.issues.append(issue)
        agg.version_state[data["version"]] = "issued"
        agg.state_transitions.append((event.at, PermitState.SIGNED))
        for raw in data["grants"]:
            grant = Grant(
                grant_id=raw["grant_id"],
                kind=ApprovalKind(raw["kind"]),
                item_id=raw["item_id"],
                introduced_version=data["version"],
                issued_at=event.at,
                basis={
                    "issue_event_id": event.event_id,
                    "issued_at": event.at.isoformat(),
                    "issued_by": event.actor,
                    "rules_version": data["rules_version"],
                    "rule_check_event_id": raw["basis"]["rule_check_event_id"],
                    "countersign_event_ids": raw["basis"]["countersign_event_ids"],
                    "permit_number": data["permit_number"],
                    "introduced_reason": "issuance",
                },
            )
            agg.grants[grant.grant_id] = grant

    elif t == "GrantsInvalidated":
        for raw in data["invalidations"]:
            grant = agg.grants.get(raw["grant_id"])
            if grant is None:
                continue
            grant.status = ApprovalStatus.INVALIDATED
            grant.invalidated = {
                "at": event.at,
                "reason": raw["reason"],
                "detail": raw.get("detail", ""),
                "by_event_id": event.event_id,
                "actor": event.actor,
                "replaced_by": raw.get("replaced_by"),
                "conflict_id": raw.get("conflict_id"),
            }

    elif t == "GrantsCarried":
        for raw in data["grants"]:
            grant = agg.grants.get(raw["grant_id"])
            if grant is not None and raw["to_version"] not in grant.carried_versions:
                grant.carried_versions.append(raw["to_version"])

    elif t == "PermitSuspended":
        agg.state = PermitState.SUSPENDED
        agg.suspensions.append(
            SuspensionRecord(
                suspended_at=event.at,
                reason=data["reason"],
                actor=event.actor,
            )
        )
        agg.state_transitions.append((event.at, PermitState.SUSPENDED))

    elif t == "PermitResumed":
        agg.state = PermitState.SIGNED
        agg.state_transitions.append((event.at, PermitState.SIGNED))
        for record in reversed(agg.suspensions):
            if record.resumed_at is None:
                record.resumed_at = event.at
                record.resume_actor = event.actor
                break

    elif t == "PermitRevoked":
        agg.state = PermitState.REVOKED
        agg.state_transitions.append((event.at, PermitState.REVOKED))
        agg.revocation = {
            "at": event.at,
            "reason": data["reason"],
            "actor": event.actor,
            "event_id": event.event_id,
        }

    elif t == "PermitAmended":
        agg.amendments.append(
            {
                "at": event.at.isoformat(),
                "version": data["version"],
                "reason": data["reason"],
                "conflict_id": data.get("conflict_id"),
                "removed_window_ids": data["removed_window_ids"],
                "added_windows": data["added_windows"],
                "actor": event.actor,
                "event_id": event.event_id,
            }
        )
        for raw in data["added_grants"]:
            grant = Grant(
                grant_id=raw["grant_id"],
                kind=ApprovalKind(raw["kind"]),
                item_id=raw["item_id"],
                introduced_version=raw["version"],
                issued_at=event.at,
                basis={
                    "issue_event_id": event.event_id,
                    "issued_at": event.at.isoformat(),
                    "issued_by": event.actor,
                    "rules_version": data["rules_version"],
                    "rule_check_event_id": raw["basis"]["rule_check_event_id"],
                    "countersign_event_ids": raw["basis"]["countersign_event_ids"],
                    "permit_number": data["permit_number"],
                    "introduced_reason": data["reason"],
                    "conflict_id": data.get("conflict_id"),
                },
            )
            agg.grants[grant.grant_id] = grant


def _impact_from_dict(raw: dict[str, Any]) -> ImpactEntry:
    return ImpactEntry(
        grant_id=raw["grant_id"],
        kind=raw["kind"],
        item_id=raw["item_id"],
        effect=raw["effect"],
        reason=raw["reason"],
        replaced_by=raw.get("replaced_by"),
    )


def impact_to_dict(entry: ImpactEntry) -> dict[str, Any]:
    return {
        "grant_id": entry.grant_id,
        "kind": entry.kind,
        "item_id": entry.item_id,
        "effect": entry.effect,
        "reason": entry.reason,
        "replaced_by": entry.replaced_by,
    }


def rebuild(events: list[Event]) -> PermitAggregate:
    if not events:
        raise ValueError("无法从空事件流重建聚合")
    agg = PermitAggregate(application_id=events[0].stream_id.split(":", 1)[1])
    for event in events:
        apply_event(agg, event)
    return agg


# ---------------------------------------------------------------------------
# 道路封路与冲突单聚合
# ---------------------------------------------------------------------------


@dataclass
class ResolutionProposal:
    at: datetime
    actor: dict[str, Any]
    added_windows: list[dict[str, Any]]
    removed_window_ids: list[str]
    note: str


@dataclass
class ConflictAggregate:
    conflict_id: str
    closure_id: str = ""
    application_id: str = ""
    segment_code: str = ""
    window_id: str = ""
    original_window: dict[str, Any] | None = None
    closure: dict[str, Any] | None = None
    status: ConflictStatus = ConflictStatus.OPEN
    opened_at: datetime | None = None
    proposals: list[ResolutionProposal] = field(default_factory=list)
    response: dict[str, Any] | None = None
    approval: dict[str, Any] | None = None
    closed: dict[str, Any] | None = None

    def latest_proposal(self) -> ResolutionProposal | None:
        return self.proposals[-1] if self.proposals else None


def apply_conflict_event(agg: ConflictAggregate, event: Event) -> None:
    data = event.data
    t = event.event_type
    if t == "ConflictOpened":
        agg.closure_id = data["closure_id"]
        agg.application_id = data["application_id"]
        agg.segment_code = data["segment_code"]
        agg.window_id = data["window_id"]
        agg.original_window = data["original_window"]
        agg.closure = data["closure"]
        agg.opened_at = event.at
        agg.status = ConflictStatus.OPEN
    elif t == "ResolutionProposed":
        agg.proposals.append(
            ResolutionProposal(
                at=event.at,
                actor=event.actor,
                added_windows=data["added_windows"],
                removed_window_ids=data["removed_window_ids"],
                note=data.get("note", ""),
            )
        )
        agg.status = ConflictStatus.RESCHEDULE_PROPOSED
    elif t == "ApplicantResponded":
        agg.response = {
            "accepted": data["accepted"],
            "comment": data.get("comment", ""),
            "at": event.at.isoformat(),
            "actor": event.actor,
        }
        agg.status = (
            ConflictStatus.RESCHEDULE_PROPOSED
            if data["accepted"]
            else ConflictStatus.REJECTED_PENDING_REVIEW
        )
    elif t == "ResolutionCountersigned":
        agg.approval = {
            "at": event.at.isoformat(),
            "actor": event.actor,
            "comment": data.get("comment", ""),
            "event_id": event.event_id,
        }
    elif t == "ConflictClosed":
        agg.status = (
            ConflictStatus.RESOLVED_RESCHEDULED
            if data["outcome"] == "rescheduled"
            else ConflictStatus.RESOLVED_CANCELLED
        )
        agg.closed = {
            "outcome": data["outcome"],
            "at": event.at.isoformat(),
            "actor": event.actor,
            "note": data.get("note", ""),
            "permit_amendment_event_id": data.get("permit_amendment_event_id"),
        }


def rebuild_conflict(events: list[Event]) -> ConflictAggregate:
    agg = ConflictAggregate(conflict_id=events[0].stream_id.split(":", 1)[1])
    for event in events:
        apply_conflict_event(agg, event)
    return agg


# ---------------------------------------------------------------------------
# 跨区域互认聚合
# ---------------------------------------------------------------------------


@dataclass
class RecognitionAggregate:
    recognition_id: str
    recognizing_jurisdiction: str = ""
    origin_jurisdiction: str = ""
    source_application_id: str = ""
    state: RecognitionState = RecognitionState.RECOGNIZED
    granted_at: datetime | None = None
    scope: dict[str, Any] = field(default_factory=dict)
    grant_basis: dict[str, Any] = field(default_factory=dict)
    amendments: list[dict[str, Any]] = field(default_factory=list)
    withdrawal: dict[str, Any] | None = None
    granted_event_id: str = ""


def apply_recognition_event(agg: RecognitionAggregate, event: Event) -> None:
    data = event.data
    t = event.event_type
    if t == "RecognitionGranted":
        agg.recognizing_jurisdiction = data["recognizing_jurisdiction"]
        agg.origin_jurisdiction = data["origin_jurisdiction"]
        agg.source_application_id = data["source_application_id"]
        agg.scope = data["scope"]
        agg.grant_basis = data.get("basis", {})
        agg.granted_at = event.at
        agg.granted_event_id = event.event_id
    elif t == "RecognitionScopeAmended":
        agg.amendments.append(
            {
                "at": event.at.isoformat(),
                "scope": data["scope"],
                "reason": data.get("reason", ""),
                "actor": event.actor,
            }
        )
        agg.scope = data["scope"]
    elif t == "RecognitionWithdrawn":
        agg.state = RecognitionState.WITHDRAWN
        agg.withdrawal = {
            "at": event.at.isoformat(),
            "reason": data["reason"],
            "actor": event.actor,
        }


def rebuild_recognition(events: list[Event]) -> RecognitionAggregate:
    agg = RecognitionAggregate(recognition_id=events[0].stream_id.split(":", 1)[1])
    for event in events:
        apply_recognition_event(agg, event)
    return agg
