"""领域事件：只追加、不可变的事实记录。

服务的全部状态都可以通过按序重放事件还原；每个事件携带发生时间与操作人，
因此任意历史时点的许可状态、责任人和审批依据都可追溯。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

# 时窗在事件载荷中的传输结构
WindowDict = dict[str, str]


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("时间必须携带时区信息")
    return parsed


def _window_to_dict(window: Any) -> WindowDict:
    return {
        "segment_code": window.segment_code,
        "starts_at": window.starts_at.isoformat(),
        "ends_at": window.ends_at.isoformat(),
    }


def _window_from_dict(data: WindowDict) -> tuple[str, datetime, datetime]:
    return data["segment_code"], _dt(data["starts_at"]), _dt(data["ends_at"])


@dataclass(frozen=True)
class Event:
    at: datetime
    actor: str

    def to_dict(self) -> dict:
        raise NotImplementedError

    @classmethod
    def from_dict(cls, data: dict) -> "Event":
        raise NotImplementedError


@dataclass(frozen=True)
class ApplicationSubmitted(Event):
    application_id: str
    version: int
    fleet_id: str
    jurisdiction: str
    submission_id: str
    vehicle_specs: dict[str, dict] = field(default_factory=dict)
    driver_authorizations: dict[str, dict] = field(default_factory=dict)
    capability: dict = field(default_factory=dict)
    route_windows: tuple = ()
    supersedes_submission_id: str | None = None
    superseded_permit_ids: tuple[str, ...] = ()
    changed_vehicles: tuple[str, ...] = ()
    changed_segments: tuple[str, ...] = ()
    duplicate_of_submission_id: str | None = None
    idempotency_key: str | None = None

    def to_dict(self) -> dict:
        return {
            "type": type(self).__name__,
            "at": self.at.isoformat(),
            "actor": self.actor,
            "application_id": self.application_id,
            "version": self.version,
            "fleet_id": self.fleet_id,
            "jurisdiction": self.jurisdiction,
            "submission_id": self.submission_id,
            "vehicle_specs": self.vehicle_specs,
            "driver_authorizations": self.driver_authorizations,
            "capability": self.capability,
            "route_windows": [_window_to_dict(w) for w in self.route_windows],
            "supersedes_submission_id": self.supersedes_submission_id,
            "superseded_permit_ids": list(self.superseded_permit_ids),
            "changed_vehicles": list(self.changed_vehicles),
            "changed_segments": list(self.changed_segments),
            "duplicate_of_submission_id": self.duplicate_of_submission_id,
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ApplicationSubmitted":
        from permit_coordination.contracts import RouteWindow

        windows = tuple(RouteWindow(*_window_from_dict(w)) for w in d["route_windows"])
        return cls(
            at=_dt(d["at"]), actor=d["actor"],
            application_id=d["application_id"], version=d["version"],
            fleet_id=d["fleet_id"], jurisdiction=d["jurisdiction"],
            submission_id=d["submission_id"],
            vehicle_specs=d.get("vehicle_specs", {}),
            driver_authorizations=d.get("driver_authorizations", {}),
            capability=d.get("capability", {}),
            route_windows=windows,
            supersedes_submission_id=d.get("supersedes_submission_id"),
            superseded_permit_ids=tuple(d.get("superseded_permit_ids", [])),
            changed_vehicles=tuple(d.get("changed_vehicles", [])),
            changed_segments=tuple(d.get("changed_segments", [])),
            duplicate_of_submission_id=d.get("duplicate_of_submission_id"),
            idempotency_key=d.get("idempotency_key"),
        )


@dataclass(frozen=True)
class ReviewOpened(Event):
    application_id: str
    version: int
    review_id: str
    jurisdiction: str

    def to_dict(self) -> dict:
        return {
            "type": type(self).__name__, "at": self.at.isoformat(), "actor": self.actor,
            "application_id": self.application_id, "version": self.version,
            "review_id": self.review_id, "jurisdiction": self.jurisdiction,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewOpened":
        return cls(at=_dt(d["at"]), actor=d["actor"], application_id=d["application_id"],
                   version=d["version"], review_id=d["review_id"], jurisdiction=d["jurisdiction"])


@dataclass(frozen=True)
class SupplementRequested(Event):
    application_id: str
    version: int
    review_id: str
    jurisdiction: str
    findings: tuple[dict, ...]
    requested_items: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "type": type(self).__name__, "at": self.at.isoformat(), "actor": self.actor,
            "application_id": self.application_id, "version": self.version,
            "review_id": self.review_id, "jurisdiction": self.jurisdiction,
            "findings": list(self.findings), "requested_items": list(self.requested_items),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SupplementRequested":
        return cls(at=_dt(d["at"]), actor=d["actor"], application_id=d["application_id"],
                   version=d["version"], review_id=d["review_id"], jurisdiction=d["jurisdiction"],
                   findings=tuple(d.get("findings", [])),
                   requested_items=tuple(d.get("requested_items", [])))


@dataclass(frozen=True)
class SupplementSubmitted(Event):
    application_id: str
    version: int
    review_id: str
    documents: tuple[dict, ...]
    resolved_finding_codes: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "type": type(self).__name__, "at": self.at.isoformat(), "actor": self.actor,
            "application_id": self.application_id, "version": self.version,
            "review_id": self.review_id, "documents": list(self.documents),
            "resolved_finding_codes": list(self.resolved_finding_codes),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SupplementSubmitted":
        return cls(at=_dt(d["at"]), actor=d["actor"], application_id=d["application_id"],
                   version=d["version"], review_id=d["review_id"],
                   documents=tuple(d.get("documents", [])),
                   resolved_finding_codes=tuple(d.get("resolved_finding_codes", [])))


@dataclass(frozen=True)
class ReviewApproved(Event):
    application_id: str
    version: int
    review_id: str
    jurisdiction: str
    conditions: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "type": type(self).__name__, "at": self.at.isoformat(), "actor": self.actor,
            "application_id": self.application_id, "version": self.version,
            "review_id": self.review_id, "jurisdiction": self.jurisdiction,
            "conditions": list(self.conditions),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewApproved":
        return cls(at=_dt(d["at"]), actor=d["actor"], application_id=d["application_id"],
                   version=d["version"], review_id=d["review_id"], jurisdiction=d["jurisdiction"],
                   conditions=tuple(d.get("conditions", [])))


@dataclass(frozen=True)
class ReviewRejected(Event):
    application_id: str
    version: int
    review_id: str
    jurisdiction: str
    reason: str

    def to_dict(self) -> dict:
        return {
            "type": type(self).__name__, "at": self.at.isoformat(), "actor": self.actor,
            "application_id": self.application_id, "version": self.version,
            "review_id": self.review_id, "jurisdiction": self.jurisdiction, "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewRejected":
        return cls(at=_dt(d["at"]), actor=d["actor"], application_id=d["application_id"],
                   version=d["version"], review_id=d["review_id"], jurisdiction=d["jurisdiction"],
                   reason=d["reason"])


@dataclass(frozen=True)
class PermitIssued(Event):
    permit_id: str
    application_id: str
    version: int
    fleet_id: str
    issuing_jurisdiction: str
    approving_jurisdictions: tuple[str, ...]
    vehicle_ids: tuple[str, ...]
    driver_ids: tuple[str, ...]
    route_windows: tuple
    review_basis: tuple[dict, ...]
    validity_start: datetime
    validity_end: datetime

    def to_dict(self) -> dict:
        return {
            "type": type(self).__name__, "at": self.at.isoformat(), "actor": self.actor,
            "permit_id": self.permit_id, "application_id": self.application_id,
            "version": self.version, "fleet_id": self.fleet_id,
            "issuing_jurisdiction": self.issuing_jurisdiction,
            "approving_jurisdictions": list(self.approving_jurisdictions),
            "vehicle_ids": list(self.vehicle_ids), "driver_ids": list(self.driver_ids),
            "route_windows": [_window_to_dict(w) for w in self.route_windows],
            "review_basis": list(self.review_basis),
            "validity_start": self.validity_start.isoformat(),
            "validity_end": self.validity_end.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PermitIssued":
        from permit_coordination.contracts import RouteWindow

        windows = tuple(RouteWindow(*_window_from_dict(w)) for w in d["route_windows"])
        return cls(
            at=_dt(d["at"]), actor=d["actor"], permit_id=d["permit_id"],
            application_id=d["application_id"], version=d["version"], fleet_id=d["fleet_id"],
            issuing_jurisdiction=d["issuing_jurisdiction"],
            approving_jurisdictions=tuple(d.get("approving_jurisdictions", [])),
            vehicle_ids=tuple(d["vehicle_ids"]), driver_ids=tuple(d["driver_ids"]),
            route_windows=windows, review_basis=tuple(d.get("review_basis", [])),
            validity_start=_dt(d["validity_start"]), validity_end=_dt(d["validity_end"]),
        )


@dataclass(frozen=True)
class PermitSuspended(Event):
    permit_id: str
    reason: str

    def to_dict(self) -> dict:
        return {"type": type(self).__name__, "at": self.at.isoformat(), "actor": self.actor,
                "permit_id": self.permit_id, "reason": self.reason}

    @classmethod
    def from_dict(cls, d: dict) -> "PermitSuspended":
        return cls(at=_dt(d["at"]), actor=d["actor"], permit_id=d["permit_id"], reason=d["reason"])


@dataclass(frozen=True)
class PermitReinstated(Event):
    permit_id: str

    def to_dict(self) -> dict:
        return {"type": type(self).__name__, "at": self.at.isoformat(), "actor": self.actor,
                "permit_id": self.permit_id}

    @classmethod
    def from_dict(cls, d: dict) -> "PermitReinstated":
        return cls(at=_dt(d["at"]), actor=d["actor"], permit_id=d["permit_id"])


@dataclass(frozen=True)
class PermitRevoked(Event):
    permit_id: str
    reason: str

    def to_dict(self) -> dict:
        return {"type": type(self).__name__, "at": self.at.isoformat(), "actor": self.actor,
                "permit_id": self.permit_id, "reason": self.reason}

    @classmethod
    def from_dict(cls, d: dict) -> "PermitRevoked":
        return cls(at=_dt(d["at"]), actor=d["actor"], permit_id=d["permit_id"], reason=d["reason"])


@dataclass(frozen=True)
class PermitSuperseded(Event):
    permit_id: str
    replaced_by_permit_id: str
    changed_vehicles: tuple[str, ...]
    changed_segments: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict:
        return {
            "type": type(self).__name__, "at": self.at.isoformat(), "actor": self.actor,
            "permit_id": self.permit_id, "replaced_by_permit_id": self.replaced_by_permit_id,
            "changed_vehicles": list(self.changed_vehicles),
            "changed_segments": list(self.changed_segments), "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PermitSuperseded":
        return cls(at=_dt(d["at"]), actor=d["actor"], permit_id=d["permit_id"],
                   replaced_by_permit_id=d["replaced_by_permit_id"],
                   changed_vehicles=tuple(d.get("changed_vehicles", [])),
                   changed_segments=tuple(d.get("changed_segments", [])),
                   reason=d["reason"])


@dataclass(frozen=True)
class RecognitionRecorded(Event):
    recognition_id: str
    permit_id: str
    recognizing_jurisdiction: str
    vehicle_ids: tuple[str, ...]
    driver_ids: tuple[str, ...]
    segment_codes: tuple[str, ...]
    window_start: datetime
    window_end: datetime
    basis: str

    def to_dict(self) -> dict:
        return {
            "type": type(self).__name__, "at": self.at.isoformat(), "actor": self.actor,
            "recognition_id": self.recognition_id, "permit_id": self.permit_id,
            "recognizing_jurisdiction": self.recognizing_jurisdiction,
            "vehicle_ids": list(self.vehicle_ids), "driver_ids": list(self.driver_ids),
            "segment_codes": list(self.segment_codes),
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(), "basis": self.basis,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RecognitionRecorded":
        return cls(at=_dt(d["at"]), actor=d["actor"], recognition_id=d["recognition_id"],
                   permit_id=d["permit_id"],
                   recognizing_jurisdiction=d["recognizing_jurisdiction"],
                   vehicle_ids=tuple(d["vehicle_ids"]), driver_ids=tuple(d["driver_ids"]),
                   segment_codes=tuple(d["segment_codes"]),
                   window_start=_dt(d["window_start"]), window_end=_dt(d["window_end"]),
                   basis=d["basis"])


@dataclass(frozen=True)
class RecognitionRevoked(Event):
    recognition_id: str
    permit_id: str
    reason: str

    def to_dict(self) -> dict:
        return {"type": type(self).__name__, "at": self.at.isoformat(), "actor": self.actor,
                "recognition_id": self.recognition_id, "permit_id": self.permit_id,
                "reason": self.reason}

    @classmethod
    def from_dict(cls, d: dict) -> "RecognitionRevoked":
        return cls(at=_dt(d["at"]), actor=d["actor"], recognition_id=d["recognition_id"],
                   permit_id=d["permit_id"], reason=d["reason"])


@dataclass(frozen=True)
class RoadClosureRegistered(Event):
    closure_id: str
    segment_codes: tuple[str, ...]
    starts_at: datetime
    ends_at: datetime
    reason: str

    def to_dict(self) -> dict:
        return {
            "type": type(self).__name__, "at": self.at.isoformat(), "actor": self.actor,
            "closure_id": self.closure_id, "segment_codes": list(self.segment_codes),
            "starts_at": self.starts_at.isoformat(), "ends_at": self.ends_at.isoformat(),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RoadClosureRegistered":
        return cls(at=_dt(d["at"]), actor=d["actor"], closure_id=d["closure_id"],
                   segment_codes=tuple(d["segment_codes"]),
                   starts_at=_dt(d["starts_at"]), ends_at=_dt(d["ends_at"]),
                   reason=d["reason"])


@dataclass(frozen=True)
class ConflictDetected(Event):
    conflict_id: str
    closure_id: str
    permit_id: str
    segment_code: str
    window_start: datetime
    window_end: datetime
    closure_start: datetime
    closure_end: datetime

    def to_dict(self) -> dict:
        return {
            "type": type(self).__name__, "at": self.at.isoformat(), "actor": self.actor,
            "conflict_id": self.conflict_id, "closure_id": self.closure_id,
            "permit_id": self.permit_id, "segment_code": self.segment_code,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "closure_start": self.closure_start.isoformat(),
            "closure_end": self.closure_end.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ConflictDetected":
        return cls(at=_dt(d["at"]), actor=d["actor"], conflict_id=d["conflict_id"],
                   closure_id=d["closure_id"], permit_id=d["permit_id"],
                   segment_code=d["segment_code"],
                   window_start=_dt(d["window_start"]), window_end=_dt(d["window_end"]),
                   closure_start=_dt(d["closure_start"]), closure_end=_dt(d["closure_end"]))


@dataclass(frozen=True)
class ConflictResolved(Event):
    conflict_id: str
    resolution: str  # rescheduled | cancelled
    new_window: Any | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "type": type(self).__name__, "at": self.at.isoformat(), "actor": self.actor,
            "conflict_id": self.conflict_id, "resolution": self.resolution,
            "new_window": _window_to_dict(self.new_window) if self.new_window else None,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ConflictResolved":
        from permit_coordination.contracts import RouteWindow

        new_window = RouteWindow(*_window_from_dict(d["new_window"])) if d.get("new_window") else None
        return cls(at=_dt(d["at"]), actor=d["actor"], conflict_id=d["conflict_id"],
                   resolution=d["resolution"], new_window=new_window, note=d.get("note", ""))


_REGISTRY: dict[str, type[Event]] = {}


def _register() -> None:
    import sys as _sys

    module = _sys.modules[__name__]
    for _name in (
        "ApplicationSubmitted ReviewOpened SupplementRequested SupplementSubmitted "
        "ReviewApproved ReviewRejected PermitIssued PermitSuspended PermitReinstated "
        "PermitRevoked PermitSuperseded RecognitionRecorded RecognitionRevoked "
        "RoadClosureRegistered ConflictDetected ConflictResolved"
    ).split():
        _REGISTRY[_name] = getattr(module, _name)


_register()


def event_from_dict(data: dict) -> Event:
    cls = _REGISTRY.get(data["type"])
    if cls is None:
        raise ValueError(f"未知事件类型: {data['type']}")
    return cls.from_dict(data)
