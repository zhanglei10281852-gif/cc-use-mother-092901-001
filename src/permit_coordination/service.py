"""许可协同领域服务：所有用例都以"校验决策 + 追加事件"实现。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

from . import aggregate as agg_mod
from .clock import Clock, SystemClock
from .contracts import (
    ApprovalKind,
    ApplicationVersion,
    ApprovalStatus,
    DriverAuthorization,
    PermitState,
    RecognitionState,
    RouteWindow,
    TestCapability,
    VehicleQualification,
    content_hash,
)
from .errors import (
    ConflictError,
    NotFoundError,
    OccupancyConflictError,
    RuleViolationError,
    ValidationError,
    WorkflowError,
)
from .event_store import Actor, EventStore
from .rules import JurisdictionRules, get_rules
from .serialization import (
    capability_from_dict,
    capability_to_dict,
    driver_from_dict,
    driver_to_dict,
    parse_dt,
    vehicle_from_dict,
    vehicle_to_dict,
    window_from_dict,
    window_to_dict,
)

# 各辖区要求的会签角色（顺序即会签顺序，但不强制）
_COUNTERSIGN_ROLES: dict[str, tuple[str, ...]] = {
    "SH_DEMO": ("safety_officer", "traffic_police"),
    "SZ_DEMO": ("safety_officer",),
}


def required_roles(jurisdiction: str) -> tuple[str, ...]:
    try:
        return _COUNTERSIGN_ROLES[jurisdiction]
    except KeyError:
        raise ValidationError(f"辖区 {jurisdiction} 未配置会签角色")


# ---------------------------------------------------------------------------
# 条目身份：同一辆车资质内容变化会得到不同 item_id，从而暴露失效关系
# ---------------------------------------------------------------------------


def vehicle_item_id(vehicle: VehicleQualification) -> str:
    return f"vehicle:{vehicle.vehicle_id}@{vehicle.qual_hash}"


def driver_item_id(driver: DriverAuthorization) -> str:
    return f"driver:{driver.driver_id}@{driver.auth_hash}"


def capability_item_id(cap: TestCapability) -> str:
    digest = content_hash([cap.code, cap.cert_ref, cap.valid_until.isoformat()])
    return f"capability:{cap.code}@{digest}"


def window_item_id(window: RouteWindow) -> str:
    return f"route:{window.segment_code}:{window.window_id}"


def _snapshot_index(snapshot: ApplicationVersion) -> dict[str, tuple[ApprovalKind, str, Any]]:
    """item_id -> (kind, base_id, 值对象)。"""
    index: dict[str, tuple[ApprovalKind, str, Any]] = {}
    for vehicle in snapshot.vehicles:
        index[vehicle_item_id(vehicle)] = (ApprovalKind.VEHICLE, f"vehicle:{vehicle.vehicle_id}", vehicle)
    for driver in snapshot.drivers:
        index[driver_item_id(driver)] = (ApprovalKind.DRIVER, f"driver:{driver.driver_id}", driver)
    for cap in snapshot.capabilities:
        index[capability_item_id(cap)] = (ApprovalKind.CAPABILITY, f"capability:{cap.code}", cap)
    for window in snapshot.route_windows:
        index[window_item_id(window)] = (
            ApprovalKind.ROUTE_WINDOW,
            f"route:{window.segment_code}",
            window,
        )
    return index


def _payload_for_item(kind: ApprovalKind, value: Any) -> dict[str, Any]:
    if kind == ApprovalKind.VEHICLE:
        return vehicle_to_dict(value)
    if kind == ApprovalKind.DRIVER:
        return driver_to_dict(value)
    if kind == ApprovalKind.CAPABILITY:
        return capability_to_dict(value)
    return window_to_dict(value)


class PermitService:
    def __init__(self, store: EventStore, clock: Clock | None = None):
        self.store = store
        self.clock = clock or SystemClock()

    # ======================================================================
    # 申请与版本提交
    # ======================================================================

    def create_application(
        self,
        application_id: str,
        fleet_id: str,
        jurisdiction: str,
        responsible: dict[str, str],
        actor: Actor,
    ) -> dict[str, Any]:
        get_rules(jurisdiction)  # 辖区必须已配置
        stream = f"app:{application_id}"
        if self.store.exists(stream):
            raise ConflictError(f"申请 {application_id} 已存在")
        for key in ("person_id", "name"):
            if not isinstance(responsible.get(key), str) or not responsible[key].strip():
                raise ValidationError(f"responsible.{key} 必须是非空字符串")
        event = self.store.append(
            stream,
            "ApplicationCreated",
            {
                "fleet_id": fleet_id,
                "jurisdiction": jurisdiction,
                "responsible": {
                    "person_id": responsible["person_id"],
                    "name": responsible["name"],
                    "contact": responsible.get("contact", ""),
                },
            },
            actor,
            expected_version=0,
        )
        return {"application_id": application_id, "created_event_id": event.event_id}

    def submit_version(
        self,
        application_id: str,
        payload: dict[str, Any],
        actor: Actor,
    ) -> dict[str, Any]:
        agg = self._load(application_id)
        if agg.state == PermitState.REVOKED:
            raise WorkflowError("许可已撤销，不允许再提交版本，请新建申请")
        if agg.state == PermitState.SUSPENDED:
            raise WorkflowError("许可暂停中，请先恢复或完成整改流程后再提交修订版本")

        vehicles = tuple(vehicle_from_dict(v) for v in _as_list(payload, "vehicles"))
        drivers = tuple(driver_from_dict(d) for d in _as_list(payload, "drivers"))
        capabilities = tuple(capability_from_dict(c) for c in _as_list(payload, "capabilities"))
        windows = tuple(window_from_dict(w) for w in _as_list(payload, "route_windows"))
        self._validate_unique(vehicles, "vehicle_id")
        self._validate_unique(drivers, "driver_id")
        self._validate_unique(capabilities, "code")
        self._validate_unique(windows, "window_id")
        self._assert_no_internal_window_overlap(windows)
        note = str(payload.get("note", ""))

        snapshot_data = {
            "vehicles": [vehicle_to_dict(v) for v in vehicles],
            "drivers": [driver_to_dict(d) for d in drivers],
            "capabilities": [capability_to_dict(c) for c in capabilities],
            "route_windows": [window_to_dict(w) for w in windows],
            "note": note,
        }
        digest = content_hash(snapshot_data)

        idem_key = payload.get("idempotency_key") or ""
        if idem_key and idem_key in agg.idempotency:
            return {
                "application_id": application_id,
                "version": agg.idempotency[idem_key],
                "deduplicated": True,
                "reason": "相同幂等键的提交已存在",
            }

        if agg.current_version and agg.versions[agg.current_version].content_digest == digest:
            raise ConflictError(
                "提交内容与当前版本完全一致，未生成新版本",
                {"version": agg.current_version, "content_digest": digest},
            )

        version_no = agg.current_version + 1
        snapshot = ApplicationVersion(
            version=version_no,
            vehicles=vehicles,
            drivers=drivers,
            capabilities=capabilities,
            route_windows=windows,
            note=note,
            content_digest=digest,
            submitted_at=self.clock.now(),
        )

        impact = self._impact_analysis(agg, snapshot)
        expected = self.store.stream_version(f"app:{application_id}")
        event = self.store.append(
            f"app:{application_id}",
            "VersionSubmitted",
            {
                "snapshot": agg_mod.version_to_data(snapshot),
                "idempotency_key": idem_key,
                "impact": [agg_mod.impact_to_dict(e) for e in impact],
            },
            actor,
            expected_version=expected,
        )

        # 新版本使旧会签失效：会签是针对具体内容作出的
        if agg.countersignatures:
            self.store.append(
                f"app:{application_id}",
                "CountersignaturesReset",
                {"version": version_no, "roles": sorted(agg.countersignatures)},
                actor,
            )

        return {
            "application_id": application_id,
            "version": version_no,
            "content_digest": digest,
            "submitted_event_id": event.event_id,
            "impact": [agg_mod.impact_to_dict(e) for e in impact],
            "deduplicated": False,
        }

    def _impact_analysis(
        self, agg: agg_mod.PermitAggregate, new_snapshot: ApplicationVersion
    ) -> list[agg_mod.ImpactEntry]:
        """对照当前仍有效的批准，说明新版本会带来什么影响。"""
        if not agg.issues:
            return []
        new_index = _snapshot_index(new_snapshot)
        new_by_base: dict[str, str] = {}
        for item_id, (_, base, _) in new_index.items():
            new_by_base.setdefault(base, item_id)

        entries: list[agg_mod.ImpactEntry] = []
        seen_new: set[str] = set()
        for grant in agg.grants.values():
            if grant.status != ApprovalStatus.ACTIVE:
                continue
            if grant.item_id in new_index:
                entries.append(agg_mod.ImpactEntry(
                    grant_id=grant.grant_id,
                    kind=grant.kind.value,
                    item_id=grant.item_id,
                    effect="carried",
                    reason="新版本中内容未变化，既有批准继续有效",
                ))
                seen_new.add(grant.item_id)
                continue
            base = _base_of(grant.item_id)
            replacement = new_by_base.get(base)
            if replacement:
                entries.append(agg_mod.ImpactEntry(
                    grant_id=grant.grant_id,
                    kind=grant.kind.value,
                    item_id=grant.item_id,
                    effect="changed",
                    reason=_change_reason(grant.kind),
                    replaced_by=replacement,
                ))
            else:
                entries.append(agg_mod.ImpactEntry(
                    grant_id=grant.grant_id,
                    kind=grant.kind.value,
                    item_id=grant.item_id,
                    effect="removed",
                    reason="该条目已从新版本中删除",
                ))
        for item_id, (kind, _, _) in new_index.items():
            if item_id in seen_new:
                continue
            already_granted = any(
                g.item_id == item_id and g.status == ApprovalStatus.ACTIVE
                for g in agg.grants.values()
            )
            if not already_granted:
                entries.append(agg_mod.ImpactEntry(
                    grant_id="",
                    kind=kind.value,
                    item_id=item_id,
                    effect="added",
                    reason="新版本新增条目，需要审查通过后才生效",
                ))
        return entries

    # ======================================================================
    # 审查：补件、规则校验、会签
    # ======================================================================

    def request_info(
        self, application_id: str, questions: list[str], actor: Actor
    ) -> dict[str, Any]:
        agg = self._load(application_id)
        if agg.state not in (PermitState.REVIEW, PermitState.INFO_REQUESTED):
            raise WorkflowError(f"当前状态 {agg.state.value} 不要求补件")
        clean = [q for q in questions if isinstance(q, str) and q.strip()]
        if not clean:
            raise ValidationError("questions 至少包含一条补件要求")
        event = self.store.append(
            f"app:{application_id}",
            "InfoRequested",
            {"version": agg.current_version, "questions": clean},
            actor,
            expected_version=self.store.stream_version(f"app:{application_id}"),
        )
        return {"info_request_event_id": event.event_id, "version": agg.current_version}

    def record_rule_check(self, application_id: str, actor: Actor) -> dict[str, Any]:
        agg = self._load(application_id)
        self._require_reviewable(agg)
        rules = get_rules(agg.jurisdiction)
        snapshot = agg.snapshot()
        at = self.clock.now()
        findings = rules.evaluate(snapshot, at)
        event = self.store.append(
            f"app:{application_id}",
            "RuleCheckRecorded",
            {
                "version": snapshot.version,
                "rules_version": rules.rules_version,
                "findings": findings,
            },
            actor,
        )
        return {
            "rule_check_event_id": event.event_id,
            "version": snapshot.version,
            "rules_version": rules.rules_version,
            "findings": findings,
            "blocker_count": len([f for f in findings if f["severity"] == "blocker"]),
        }

    def countersign(
        self, application_id: str, comment: str, actor: Actor
    ) -> dict[str, Any]:
        agg = self._load(application_id)
        self._require_reviewable(agg)
        roles = required_roles(agg.jurisdiction)
        if actor.role not in roles:
            raise WorkflowError(
                f"{actor.role} 不是 {agg.jurisdiction} 的会签角色",
                {"required_roles": list(roles)},
            )
        version = agg.current_version
        existing = agg.countersignatures.get(actor.role)
        if existing is not None and existing.version == version:
            raise ConflictError(f"{actor.role} 已完成本版本会签")
        check = agg.latest_rule_check(version)
        if check is None:
            raise WorkflowError("本版本尚未完成辖区规则校验，不能会签")
        if check.blockers():
            raise RuleViolationError("规则校验仍有 blocker，不能会签", check.blockers())
        event = self.store.append(
            f"app:{application_id}",
            "Countersigned",
            {"role": actor.role, "version": version, "comment": comment or ""},
            actor,
        )
        return {"countersign_event_id": event.event_id, "role": actor.role, "version": version}

    # ======================================================================
    # 签发 / 重新签发
    # ======================================================================

    def issue_permit(self, application_id: str, actor: Actor) -> dict[str, Any]:
        with self.store.transaction():
            return self._issue_permit_locked(application_id, actor)

    def _issue_permit_locked(self, application_id: str, actor: Actor) -> dict[str, Any]:
        agg = self._load(application_id)
        self._require_reviewable(agg)
        version = agg.current_version
        snapshot = agg.snapshot(version)

        check = agg.latest_rule_check(version)
        if check is None:
            raise WorkflowError("本版本尚未完成辖区规则校验")
        if check.blockers():
            raise RuleViolationError("规则校验存在 blocker，不能签发", check.blockers())

        roles = required_roles(agg.jurisdiction)
        missing = [
            role
            for role in roles
            if (sig := agg.countersignatures.get(role)) is None or sig.version != version
        ]
        if missing:
            raise WorkflowError("会签不完整，不能签发", {"missing_roles": missing})

        index = _snapshot_index(snapshot)
        active = [g for g in agg.grants.values() if g.status == ApprovalStatus.ACTIVE]
        active_by_item = {g.item_id: g for g in active}

        invalidations: list[dict[str, Any]] = []
        carries: list[dict[str, Any]] = []
        grant_payloads: list[dict[str, Any]] = []
        new_windows: list[RouteWindow] = []

        for grant in active:
            if grant.item_id in index:
                carries.append({"grant_id": grant.grant_id, "to_version": version})
                continue
            base = _base_of(grant.item_id)
            replacement = next(
                (item_id for item_id, (_, b, _) in index.items() if b == base),
                None,
            )
            invalidations.append({
                "grant_id": grant.grant_id,
                "reason": "item_changed" if replacement else "removed_in_new_version",
                "detail": _change_reason(grant.kind) if replacement else "条目已从新版本删除",
                "replaced_by": replacement,
            })

        for item_id, (kind, _, value) in index.items():
            if item_id in active_by_item:
                continue
            grant_payloads.append({
                "grant_id": f"grant_{content_hash([application_id, item_id, str(version)])}",
                "kind": kind.value,
                "item_id": item_id,
                "payload": _payload_for_item(kind, value),
            })
            if kind == ApprovalKind.ROUTE_WINDOW:
                new_windows.append(value)

        # 占用冲突：仅检查新进入许可的时窗（沿用时窗在先前签发时已检查过）
        self._assert_no_occupancy_conflict(application_id, new_windows, at=self.clock.now())

        permit_number = self._permit_number(agg)
        countersign_ids = [
            agg.countersignatures[role].event_id for role in roles
        ]
        rules = get_rules(agg.jurisdiction)
        expected = self.store.stream_version(f"app:{application_id}")

        if invalidations:
            self.store.append(
                f"app:{application_id}",
                "GrantsInvalidated",
                {"version": version, "invalidations": invalidations},
                actor,
                expected_version=expected,
            )
            expected += 1
        if carries:
            self.store.append(
                f"app:{application_id}",
                "GrantsCarried",
                {"version": version, "grants": carries},
                actor,
                expected_version=expected,
            )
            expected += 1

        event = self.store.append(
            f"app:{application_id}",
            "PermitIssued",
            {
                "version": version,
                "permit_number": permit_number,
                "rules_version": rules.rules_version,
                "grants": [
                    {
                        "grant_id": g["grant_id"],
                        "kind": g["kind"],
                        "item_id": g["item_id"],
                        "payload": g["payload"],
                        "basis": {
                            "rule_check_event_id": check.event_id,
                            "countersign_event_ids": countersign_ids,
                        },
                    }
                    for g in grant_payloads
                ],
                "invalidated_count": len(invalidations),
                "carried_count": len(carries),
            },
            actor,
            expected_version=expected,
        )
        return {
            "permit_number": permit_number,
            "application_id": application_id,
            "version": version,
            "issue_event_id": event.event_id,
            "new_grant_count": len(grant_payloads),
            "invalidated_count": len(invalidations),
            "carried_count": len(carries),
        }

    def _permit_number(self, agg: agg_mod.PermitAggregate) -> str:
        if agg.issues:
            return agg.issues[-1].permit_number
        count = len(self.store.events_of_type("PermitIssued")) + 1
        return f"{agg.jurisdiction}-P-{count:04d}"

    # ======================================================================
    # 暂停 / 恢复 / 撤销
    # ======================================================================

    def suspend(self, application_id: str, reason: str, actor: Actor) -> dict[str, Any]:
        agg = self._load(application_id)
        # 已签发（即使修订版本正在审查中）且未撤销/未暂停，即可暂停
        if not agg.issues or agg.revocation is not None or agg.state == PermitState.SUSPENDED:
            raise WorkflowError(f"当前状态 {agg.state.value} 不能暂停")
        if not reason.strip():
            raise ValidationError("暂停原因不能为空")
        event = self.store.append(
            f"app:{application_id}",
            "PermitSuspended",
            {"reason": reason},
            actor,
        )
        return {"suspension_event_id": event.event_id}

    def resume(self, application_id: str, comment: str, actor: Actor) -> dict[str, Any]:
        agg = self._load(application_id)
        if agg.state != PermitState.SUSPENDED:
            raise WorkflowError(f"只有暂停中的许可可以恢复，当前 {agg.state.value}")
        event = self.store.append(
            f"app:{application_id}",
            "PermitResumed",
            {"comment": comment or ""},
            actor,
        )
        return {"resumption_event_id": event.event_id}

    def revoke(self, application_id: str, reason: str, actor: Actor) -> dict[str, Any]:
        agg = self._load(application_id)
        if agg.state not in (PermitState.SIGNED, PermitState.SUSPENDED):
            raise WorkflowError(f"当前状态 {agg.state.value} 不能撤销")
        if not reason.strip():
            raise ValidationError("撤销原因不能为空")
        stream = f"app:{application_id}"
        expected = self.store.stream_version(stream)
        active = [g for g in agg.grants.values() if g.status == ApprovalStatus.ACTIVE]
        if active:
            self.store.append(
                stream,
                "GrantsInvalidated",
                {
                    "version": agg.current_version,
                    "invalidations": [
                        {
                            "grant_id": g.grant_id,
                            "reason": "permit_revoked",
                            "detail": reason,
                        }
                        for g in active
                    ],
                },
                actor,
                expected_version=expected,
            )
            expected += 1
        event = self.store.append(
            stream,
            "PermitRevoked",
            {"reason": reason},
            actor,
            expected_version=expected,
        )
        return {"revocation_event_id": event.event_id}

    # ======================================================================
    # 临时封路 → 冲突单 → 改期
    # ======================================================================

    def register_closure(self, payload: dict[str, Any], actor: Actor) -> dict[str, Any]:
        with self.store.transaction():
            return self._register_closure_locked(payload, actor)

    def _register_closure_locked(self, payload: dict[str, Any], actor: Actor) -> dict[str, Any]:
        closure_id = str(payload.get("closure_id", "")).strip()
        if not closure_id:
            raise ValidationError("closure_id 不能为空")
        stream = f"closure:{closure_id}"
        if self.store.exists(stream):
            raise ConflictError(f"封路记录 {closure_id} 已存在")
        window = window_from_dict(
            {
                "segment_code": payload.get("segment_code"),
                "starts_at": payload.get("starts_at"),
                "ends_at": payload.get("ends_at"),
            }
        )
        reason = str(payload.get("reason", "")).strip()
        if not reason:
            raise ValidationError("封路原因不能为空")

        event = self.store.append(
            stream,
            "RoadClosureRegistered",
            {
                "closure_id": closure_id,
                "segment_code": window.segment_code,
                "starts_at": window.starts_at.isoformat(),
                "ends_at": window.ends_at.isoformat(),
                "reason": reason,
            },
            actor,
            expected_version=0,
        )

        conflict_ids: list[str] = []
        now = self.clock.now()
        existing = self._conflict_index()
        for app_id in self._application_ids():
            app_agg = self._load(app_id)
            # 暂停中的许可同样登记冲突，便于恢复前完成改期
            if app_agg.effective_state_at(now) not in (
                PermitState.SIGNED,
                PermitState.SUSPENDED,
            ):
                continue
            for active_window in app_agg.active_windows_at(now):
                if active_window.ends_at <= now:
                    continue
                if not active_window.overlaps(window):
                    continue
                dedupe_key = (closure_id, app_id, active_window.window_id)
                if dedupe_key in existing:
                    open_conflict = existing[dedupe_key]
                    if open_conflict.startswith("CFT-"):
                        conflict_ids.append(open_conflict)
                    continue
                conflict_id = f"CFT-{content_hash([closure_id, app_id, active_window.window_id])[:10]}"
                cstream = f"conflict:{conflict_id}"
                created = self.store.append(
                    cstream,
                    "ConflictOpened",
                    {
                        "conflict_id": conflict_id,
                        "closure_id": closure_id,
                        "application_id": app_id,
                        "permit_number": app_agg.effective_issue_at(now).permit_number,  # type: ignore[union-attr]
                        "segment_code": window.segment_code,
                        "window_id": active_window.window_id,
                        "original_window": window_to_dict(active_window),
                        "closure": event.data,
                    },
                    actor,
                    expected_version=0,
                )
                conflict_ids.append(conflict_id)
                existing[dedupe_key] = conflict_id
        return {
            "closure_id": closure_id,
            "closure_event_id": event.event_id,
            "conflict_ids": conflict_ids,
            "conflict_count": len(conflict_ids),
        }

    def propose_resolution(
        self, conflict_id: str, payload: dict[str, Any], actor: Actor
    ) -> dict[str, Any]:
        conflict = self._load_conflict(conflict_id)
        if conflict.status.value not in ("open", "rejected_pending_review", "reschedule_proposed"):
            raise WorkflowError(f"冲突单当前状态 {conflict.status.value}，不能提交改期方案")
        app_agg = self._load(conflict.application_id)
        rules = get_rules(app_agg.jurisdiction)

        raw_windows = payload.get("added_windows")
        if raw_windows is None:
            proposed = [self._auto_reschedule_window(conflict, rules)]
        else:
            if not isinstance(raw_windows, list) or not raw_windows:
                raise ValidationError("added_windows 必须是非空数组，或省略由系统生成")
            proposed = [window_from_dict(w) for w in raw_windows]

        removed = [conflict.window_id]
        self._validate_resolution_windows(conflict, proposed, rules)

        event = self.store.append(
            f"conflict:{conflict_id}",
            "ResolutionProposed",
            {
                "added_windows": [window_to_dict(w) for w in proposed],
                "removed_window_ids": removed,
                "note": str(payload.get("note", "")),
            },
            actor,
        )
        return {
            "conflict_id": conflict_id,
            "proposal_event_id": event.event_id,
            "added_windows": [window_to_dict(w) for w in proposed],
            "removed_window_ids": removed,
        }

    def _auto_reschedule_window(
        self, conflict: agg_mod.ConflictAggregate, rules: JurisdictionRules
    ) -> RouteWindow:
        original = window_from_dict(conflict.original_window)  # type: ignore[arg-type]
        closure_end = parse_dt(conflict.closure["ends_at"], "closure.ends_at")  # type: ignore[index]
        now = self.clock.now()
        start = max(original.starts_at, closure_end, now)
        duration_hours = min(original.duration_hours(), rules.max_window_hours)
        from datetime import timedelta

        return RouteWindow(
            segment_code=original.segment_code,
            starts_at=start,
            ends_at=start + timedelta(hours=duration_hours),
        )

    def _validate_resolution_windows(
        self,
        conflict: agg_mod.ConflictAggregate,
        proposed: list[RouteWindow],
        rules: JurisdictionRules,
    ) -> None:
        now = self.clock.now()
        closure = conflict.closure
        assert closure is not None
        closure_window = RouteWindow(
            closure["segment_code"],
            parse_dt(closure["starts_at"], "closure.starts_at"),
            parse_dt(closure["ends_at"], "closure.ends_at"),
        )
        errors: list[dict[str, Any]] = []
        for window in proposed:
            if window.overlaps(closure_window):
                errors.append({"code": "still_closure", "window": window_to_dict(window)})
            if window.starts_at < now:
                errors.append({"code": "window_in_past", "window": window_to_dict(window)})
            if rules.allowed_segments and window.segment_code not in rules.allowed_segments:
                errors.append({"code": "segment_not_allowed", "window": window_to_dict(window)})
            if window.duration_hours() > rules.max_window_hours:
                errors.append({"code": "window_too_long", "window": window_to_dict(window)})
        # 改期时窗不能与本许可其他保留时窗自相重叠
        kept = [
            w
            for w in self._load(conflict.application_id).active_windows_at(now)
            if w.window_id != conflict.window_id
        ]
        for new_window in proposed:
            for existing in kept:
                if new_window.overlaps(existing):
                    errors.append({
                        "code": "self_permit_overlap",
                        "window": window_to_dict(new_window),
                        "other_window": window_to_dict(existing),
                    })
        occupancy = self._occupancy_conflicts(
            conflict.application_id,
            proposed,
            now,
            ignore_window_ids={conflict.window_id},
        )
        errors.extend(occupancy)
        if errors:
            raise RuleViolationError("改期方案不可用", errors)

    def respond_resolution(
        self, conflict_id: str, accepted: bool, comment: str, actor: Actor
    ) -> dict[str, Any]:
        conflict = self._load_conflict(conflict_id)
        if conflict.status != conflict.status.RESCHEDULE_PROPOSED:
            raise WorkflowError("尚无待响应的改期方案")
        if conflict.latest_proposal() is None:
            raise WorkflowError("改期方案缺失")
        event = self.store.append(
            f"conflict:{conflict_id}",
            "ApplicantResponded",
            {"accepted": bool(accepted), "comment": comment or ""},
            actor,
        )
        return {"response_event_id": event.event_id, "accepted": bool(accepted)}

    def countersign_resolution(self, conflict_id: str, comment: str, actor: Actor) -> dict[str, Any]:
        conflict = self._load_conflict(conflict_id)
        if conflict.response is None:
            raise WorkflowError("申请方尚未响应，不能会签改期")
        if not conflict.response["accepted"]:
            raise WorkflowError("申请方拒绝改期，应走取消流程")
        proposal = conflict.latest_proposal()
        assert proposal is not None
        app_agg = self._load(conflict.application_id)
        rules = get_rules(app_agg.jurisdiction)
        proposed = [window_from_dict(w) for w in proposal.added_windows]
        self._validate_resolution_windows(conflict, proposed, rules)

        # 改期时窗也要通过辖区规则（用替换后的完整时窗集合评估）
        removed = set(proposal.removed_window_ids)
        effective_windows = [
            w for w in app_agg.active_windows_at(self.clock.now()) if w.window_id not in removed
        ] + proposed
        snapshot = replace(app_agg.snapshot(), route_windows=tuple(effective_windows))
        findings = rules.evaluate(snapshot, self.clock.now())
        blockers = [f for f in findings if f["severity"] == "blocker"]
        if blockers:
            raise RuleViolationError("改期后时窗不满足辖区规则", blockers)
        event = self.store.append(
            f"conflict:{conflict_id}",
            "ResolutionCountersigned",
            {
                "comment": comment or "",
                "rules_version": rules.rules_version,
                "findings": findings,
            },
            actor,
        )
        return {"countersign_event_id": event.event_id, "findings": findings}

    def close_conflict(self, conflict_id: str, actor: Actor) -> dict[str, Any]:
        with self.store.transaction():
            return self._close_conflict_locked(conflict_id, actor)

    def _close_conflict_locked(self, conflict_id: str, actor: Actor) -> dict[str, Any]:
        conflict = self._load_conflict(conflict_id)
        if conflict.closed is not None:
            raise WorkflowError("冲突单已关闭")
        if conflict.response is None:
            raise WorkflowError("申请方尚未响应改期方案")
        proposal = conflict.latest_proposal()
        assert proposal is not None
        app_agg = self._load(conflict.application_id)
        rules = get_rules(app_agg.jurisdiction)

        if conflict.response["accepted"]:
            if conflict.approval is None:
                raise WorkflowError("改期方案尚未通过会签")
            proposed = [window_from_dict(w) for w in proposal.added_windows]
            self._validate_resolution_windows(conflict, proposed, rules)
            amendment_event_id = self._apply_reschedule_amendment(
                conflict, proposed, rules, actor
            )
            outcome = "rescheduled"
        else:
            self._apply_cancellation(conflict, actor)
            amendment_event_id = None
            outcome = "cancelled"

        event = self.store.append(
            f"conflict:{conflict_id}",
            "ConflictClosed",
            {
                "outcome": outcome,
                "note": conflict.response.get("comment", ""),
                "permit_amendment_event_id": amendment_event_id,
            },
            actor,
        )
        return {
            "conflict_id": conflict_id,
            "outcome": outcome,
            "close_event_id": event.event_id,
            "permit_amendment_event_id": amendment_event_id,
        }

    def _apply_reschedule_amendment(
        self,
        conflict: agg_mod.ConflictAggregate,
        proposed: list[RouteWindow],
        rules: JurisdictionRules,
        actor: Actor,
    ) -> str:
        stream = f"app:{conflict.application_id}"
        app_agg = self._load(conflict.application_id)
        expected = self.store.stream_version(stream)
        removed_ids = [conflict.window_id]
        old_grants = [
            g
            for g in app_agg.grants.values()
            if g.status == ApprovalStatus.ACTIVE
            and g.item_id in {
                f"route:{conflict.segment_code}:{wid}" for wid in removed_ids
            }
        ]
        if old_grants:
            self.store.append(
                stream,
                "GrantsInvalidated",
                {
                    "version": app_agg.current_version,
                    "invalidations": [
                        {
                            "grant_id": g.grant_id,
                            "reason": "road_closure_reschedule",
                            "detail": f"封路 {conflict.closure_id} 触发改期",
                            "replaced_by": window_item_id(proposed[0]),
                            "conflict_id": conflict.conflict_id,
                        }
                        for g in old_grants
                    ],
                },
                actor,
                expected_version=expected,
            )
            expected += 1

        assert conflict.approval is not None
        permit_number = app_agg.issues[-1].permit_number
        added_grants = [
            {
                "grant_id": f"grant_{content_hash([conflict.application_id, w.window_id, 'amend', conflict.conflict_id])}",
                "kind": ApprovalKind.ROUTE_WINDOW.value,
                "item_id": window_item_id(w),
                "version": app_agg.current_version,
                "basis": {
                    "rule_check_event_id": None,
                    "countersign_event_ids": [conflict.approval["event_id"]],
                },
            }
            for w in proposed
        ]
        event = self.store.append(
            stream,
            "PermitAmended",
            {
                "version": app_agg.current_version,
                "permit_number": permit_number,
                "rules_version": rules.rules_version,
                "reason": f"封路 {conflict.closure_id} 改期",
                "conflict_id": conflict.conflict_id,
                "removed_window_ids": removed_ids,
                "added_windows": [window_to_dict(w) for w in proposed],
                "added_grants": added_grants,
            },
            actor,
            expected_version=expected,
        )
        return event.event_id

    def _apply_cancellation(
        self, conflict: agg_mod.ConflictAggregate, actor: Actor
    ) -> None:
        stream = f"app:{conflict.application_id}"
        app_agg = self._load(conflict.application_id)
        old_grants = [
            g
            for g in app_agg.grants.values()
            if g.status == ApprovalStatus.ACTIVE and g.item_id == (
                f"route:{conflict.segment_code}:{conflict.window_id}"
            )
        ]
        if not old_grants:
            return
        self.store.append(
            stream,
            "GrantsInvalidated",
            {
                "version": app_agg.current_version,
                "invalidations": [
                    {
                        "grant_id": g.grant_id,
                        "reason": "road_closure_cancelled",
                        "detail": f"封路 {conflict.closure_id} 且申请方拒绝改期，时窗占用取消",
                        "conflict_id": conflict.conflict_id,
                    }
                    for g in old_grants
                ],
            },
            actor,
        )

    # ======================================================================
    # 跨区域互认
    # ======================================================================

    def grant_recognition(self, payload: dict[str, Any], actor: Actor) -> dict[str, Any]:
        with self.store.transaction():
            return self._grant_recognition_locked(payload, actor)

    def _grant_recognition_locked(self, payload: dict[str, Any], actor: Actor) -> dict[str, Any]:
        source_id = str(payload.get("source_application_id", "")).strip()
        recognizing = str(payload.get("recognizing_jurisdiction", "")).strip()
        if not source_id or not recognizing:
            raise ValidationError("source_application_id 与 recognizing_jurisdiction 必填")
        get_rules(recognizing)
        source = self._load(source_id)
        now = self.clock.now()
        if source.effective_state_at(now) != PermitState.SIGNED:
            raise WorkflowError("源许可当前不是有效签发状态，不能互认")
        issue = source.effective_issue_at(now)
        assert issue is not None

        active = source.active_grants_at(now)
        by_kind: dict[ApprovalKind, set[str]] = {}
        for grant in active:
            by_kind.setdefault(grant.kind, set()).add(grant.item_id)

        scope_raw = payload.get("scope") or {}
        if not isinstance(scope_raw, dict):
            raise ValidationError("scope 必须是对象")
        scope = self._resolve_scope(scope_raw, by_kind)

        # 认可范围必须是源许可实际范围的子集
        for kind, ids in scope.items():
            known = by_kind.get(kind, set())
            unknown = sorted(set(ids) - known)
            if unknown:
                raise ValidationError(
                    f"认可范围包含源许可未生效的 {kind.value} 条目",
                    {"unknown": unknown},
                )

        recognized_grants = {}
        for kind, ids in scope.items():
            for item_id in ids:
                grant = next(g for g in active if g.kind == kind and g.item_id == item_id)
                recognized_grants[item_id] = {
                    "grant_id": grant.grant_id,
                    "basis": grant.basis,
                }

        recognition_id = payload.get("recognition_id") or (
            f"REC-{content_hash([recognizing, source_id, now.isoformat()])[:12]}"
        )
        stream = f"recognition:{recognition_id}"
        if self.store.exists(stream):
            raise ConflictError(f"互认记录 {recognition_id} 已存在")
        scope_out = {kind.value: sorted(ids) for kind, ids in scope.items()}
        event = self.store.append(
            stream,
            "RecognitionGranted",
            {
                "recognition_id": recognition_id,
                "recognizing_jurisdiction": recognizing,
                "origin_jurisdiction": source.jurisdiction,
                "source_application_id": source_id,
                "source_permit_number": issue.permit_number,
                "scope": scope_out,
                "basis": {
                    "source_issue_event_id": issue.event_id,
                    "source_issued_at": issue.at.isoformat(),
                    "source_rules_version": issue.rules_version,
                    "recognized_grants": recognized_grants,
                    "granted_by": actor.to_dict(),
                },
            },
            actor,
            expected_version=0,
        )
        return {"recognition_id": recognition_id, "granted_event_id": event.event_id, "scope": scope_out}

    def _resolve_scope(
        self,
        scope_raw: dict[str, Any],
        by_kind: dict[ApprovalKind, set[str]],
    ) -> dict[ApprovalKind, list[str]]:
        mapping = {
            "vehicle_ids": ApprovalKind.VEHICLE,
            "driver_ids": ApprovalKind.DRIVER,
            "capability_codes": ApprovalKind.CAPABILITY,
            "window_ids": ApprovalKind.ROUTE_WINDOW,
        }
        result: dict[ApprovalKind, list[str]] = {}
        for field_name, kind in mapping.items():
            if field_name not in scope_raw or scope_raw[field_name] is None:
                result[kind] = sorted(by_kind.get(kind, set()))
                continue
            value = scope_raw[field_name]
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValidationError(f"scope.{field_name} 必须是字符串数组")
            result[kind] = value
        return result

    def amend_recognition_scope(
        self, recognition_id: str, scope_raw: dict[str, Any], reason: str, actor: Actor
    ) -> dict[str, Any]:
        rec = self._load_recognition(recognition_id)
        if rec.state != RecognitionState.RECOGNIZED:
            raise WorkflowError("互认已撤回，不能修改范围")
        source = self._load(rec.source_application_id)
        now = self.clock.now()
        active = source.active_grants_at(now)
        by_kind: dict[ApprovalKind, set[str]] = {}
        for grant in active:
            by_kind.setdefault(grant.kind, set()).add(grant.item_id)
        scope = self._resolve_scope(scope_raw, by_kind)
        # 收窄认可范围：新范围必须是旧范围的子集（扩大需重新签发互认）
        for kind, ids in scope.items():
            previous = set(rec.scope.get(kind.value, []))
            expanded = set(ids) - previous
            if expanded:
                raise WorkflowError(
                    "认可范围只能收窄，扩大范围请重新签发互认",
                    {"kind": kind.value, "expanded": sorted(expanded)},
                )
        scope_out = {kind.value: ids for kind, ids in scope.items()}
        event = self.store.append(
            f"recognition:{recognition_id}",
            "RecognitionScopeAmended",
            {"scope": scope_out, "reason": reason or ""},
            actor,
        )
        return {"amend_event_id": event.event_id, "scope": scope_out}

    def withdraw_recognition(self, recognition_id: str, reason: str, actor: Actor) -> dict[str, Any]:
        rec = self._load_recognition(recognition_id)
        if rec.state != RecognitionState.RECOGNIZED:
            raise WorkflowError("互认已处于撤回状态")
        if not reason.strip():
            raise ValidationError("撤回原因不能为空")
        event = self.store.append(
            f"recognition:{recognition_id}",
            "RecognitionWithdrawn",
            {"reason": reason},
            actor,
        )
        return {"withdrawal_event_id": event.event_id}

    # ======================================================================
    # 占用检测
    # ======================================================================

    def _assert_no_occupancy_conflict(
        self,
        application_id: str,
        windows: list[RouteWindow],
        at: datetime,
    ) -> None:
        conflicts = self._occupancy_conflicts(application_id, windows, at)
        if conflicts:
            raise OccupancyConflictError("路段时窗与其他有效许可冲突", conflicts)

    def _occupancy_conflicts(
        self,
        application_id: str,
        windows: list[RouteWindow],
        at: datetime,
        ignore_window_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        ignore_window_ids = ignore_window_ids or set()
        conflicts: list[dict[str, Any]] = []
        for window in windows:
            for closure_id, closure in self.active_closures(at):
                if window.overlaps(closure):
                    conflicts.append({
                        "code": "road_closure",
                        "window": window_to_dict(window),
                        "closure_id": closure_id,
                        "closure": {
                            "segment_code": closure.segment_code,
                            "starts_at": closure.starts_at.isoformat(),
                            "ends_at": closure.ends_at.isoformat(),
                        },
                    })
        for other_id in self._application_ids():
            if other_id == application_id:
                continue
            other = self._load(other_id)
            if other.effective_state_at(at) != PermitState.SIGNED:
                continue
            for other_window in other.active_windows_at(at):
                if other_window.window_id in ignore_window_ids:
                    continue
                for window in windows:
                    if window.overlaps(other_window):
                        issue = other.effective_issue_at(at)
                        conflicts.append({
                            "code": "permit_overlap",
                            "window": window_to_dict(window),
                            "other_application_id": other_id,
                            "other_permit_number": issue.permit_number if issue else None,
                            "other_window": window_to_dict(other_window),
                        })
        return conflicts

    def active_closures(self, at: datetime) -> list[tuple[str, RouteWindow]]:
        result: list[tuple[str, RouteWindow]] = []
        for stream_id in self.store.stream_ids("closure:"):
            events = self.store.load_stream(stream_id)
            data = events[0].data
            window = RouteWindow(
                data["segment_code"],
                parse_dt(data["starts_at"], "starts_at"),
                parse_dt(data["ends_at"], "ends_at"),
            )
            if window.ends_at > at:
                result.append((data["closure_id"], window))
        return result

    # ======================================================================
    # 读取模型 / 取证视图
    # ======================================================================

    def get_application_view(self, application_id: str, as_of: datetime | None = None) -> dict[str, Any]:
        events = self.store.load_stream(f"app:{application_id}")
        if as_of is not None:
            events = [e for e in events if e.at <= as_of]
            if not events:
                raise NotFoundError(f"时点 {as_of.isoformat()} 申请 {application_id} 尚不存在")
        agg = agg_mod.rebuild(events)
        return self._application_view(agg, as_of or self.clock.now())

    def _application_view(self, agg: agg_mod.PermitAggregate, at: datetime) -> dict[str, Any]:
        versions_out = []
        for number in sorted(agg.versions):
            snap = agg.versions[number]
            versions_out.append({
                "version": number,
                "submitted_at": snap.submitted_at.isoformat(),
                "note": snap.note,
                "content_digest": snap.content_digest,
                "vehicles": [vehicle_to_dict(v) for v in snap.vehicles],
                "drivers": [driver_to_dict(d) for d in snap.drivers],
                "capabilities": [capability_to_dict(c) for c in snap.capabilities],
                "route_windows": [window_to_dict(w) for w in snap.route_windows],
                "state": agg.version_state.get(number, "submitted"),
            })
        issue = agg.effective_issue_at(at)
        return {
            "application_id": agg.application_id,
            "fleet_id": agg.fleet_id,
            "jurisdiction": agg.jurisdiction,
            "responsible": agg.responsible,
            "state": agg.effective_state_at(at).value,
            "workflow_state": agg.state.value,
            "under_revision": bool(agg.issues) and agg.current_version > agg.issues[-1].version,
            "current_version": max(
                (n for n in agg.versions if agg.versions[n].submitted_at <= at),
                default=0,
            ),
            "permit_number": issue.permit_number if issue else None,
            "versions": versions_out,
            "grants": [
                self._grant_view(g, at)
                for g in sorted(agg.grants.values(), key=lambda g: g.grant_id)
                if g.issued_at <= at
            ],
            "countersignatures": [
                {
                    "role": sig.role,
                    "version": sig.version,
                    "at": sig.at.isoformat(),
                    "actor": sig.actor,
                    "comment": sig.comment,
                    "event_id": sig.event_id,
                }
                for sig in sorted(agg.countersignatures.values(), key=lambda s: s.at)
                if sig.at <= at
            ],
            "rule_checks": [
                {
                    "version": c.version,
                    "at": c.at.isoformat(),
                    "rules_version": c.rules_version,
                    "findings": c.findings,
                    "actor": c.actor,
                    "event_id": c.event_id,
                }
                for c in agg.rule_checks if c.at <= at
            ],
            "issues": [
                {
                    "version": i.version,
                    "permit_number": i.permit_number,
                    "at": i.at.isoformat(),
                    "actor": i.actor,
                    "rules_version": i.rules_version,
                    "event_id": i.event_id,
                }
                for i in agg.issues if i.at <= at
            ],
            "suspensions": [
                {
                    "suspended_at": s.suspended_at.isoformat(),
                    "reason": s.reason,
                    "actor": s.actor,
                    "resumed_at": s.resumed_at.isoformat() if s.resumed_at else None,
                }
                for s in agg.suspensions if s.suspended_at <= at
            ],
            "revocation": agg.revocation,
            "info_requests": [r for r in agg.info_requests if parse_dt(r["at"], "at") <= at],
            "amendments": [
                {
                    "at": a["at"],
                    "reason": a["reason"],
                    "conflict_id": a.get("conflict_id"),
                    "removed_window_ids": a["removed_window_ids"],
                    "added_windows": a["added_windows"],
                    "actor": a["actor"],
                    "event_id": a["event_id"],
                }
                for a in agg.amendments
                if parse_dt(a["at"], "at") <= at
            ],
            "last_impact": [agg_mod.impact_to_dict(e) for e in agg.last_impact],
            "as_of": at.isoformat(),
        }

    def _grant_view(self, grant: agg_mod.Grant, at: datetime) -> dict[str, Any]:
        invalidated = grant.invalidated
        invalidated_at = invalidated["at"] if invalidated else None
        return {
            "grant_id": grant.grant_id,
            "kind": grant.kind.value,
            "item_id": grant.item_id,
            "introduced_version": grant.introduced_version,
            "carried_versions": grant.carried_versions,
            "status": "active" if grant.is_active_at(at) else "invalidated",
            "issued_at": grant.issued_at.isoformat(),
            "basis": grant.basis,
            "invalidated": (
                None
                if not invalidated or invalidated_at > at
                else {
                    "at": invalidated_at.isoformat(),
                    "reason": invalidated["reason"],
                    "detail": invalidated.get("detail"),
                    "replaced_by": invalidated.get("replaced_by"),
                    "conflict_id": invalidated.get("conflict_id"),
                    "by_event_id": invalidated.get("by_event_id"),
                    "actor": invalidated.get("actor"),
                }
            ),
        }

    def effective_view(self, application_id: str, at: datetime) -> dict[str, Any]:
        """检查时点视图：当时有效的许可、责任人与审批依据。"""
        agg = self._load(application_id)
        issue = agg.effective_issue_at(at)
        state = agg.effective_state_at(at)
        windows = agg.active_windows_at(at) if state == PermitState.SIGNED else []
        grants = agg.active_grants_at(at) if state == PermitState.SIGNED else []
        enforceable = [
            w for w in windows if w.starts_at <= at < w.ends_at
        ]
        return {
            "application_id": application_id,
            "permit_number": issue.permit_number if issue else None,
            "state": state.value,
            "at": at.isoformat(),
            "responsible": agg.responsible,
            "issued_for_version": issue.version if issue else None,
            "issued_at": issue.at.isoformat() if issue else None,
            "issued_by": issue.actor if issue else None,
            "rules_version": issue.rules_version if issue else None,
            "active_grants": [
                {
                    "grant_id": g.grant_id,
                    "kind": g.kind.value,
                    "item_id": g.item_id,
                    "introduced_version": g.introduced_version,
                    "basis": g.basis,
                }
                for g in grants
            ],
            "active_route_windows": [window_to_dict(w) for w in windows],
            "enforceable_route_windows": [window_to_dict(w) for w in enforceable],
            "active_vehicles": [g.item_id for g in grants if g.kind == ApprovalKind.VEHICLE],
            "active_drivers": [g.item_id for g in grants if g.kind == ApprovalKind.DRIVER],
            "active_capabilities": [g.item_id for g in grants if g.kind == ApprovalKind.CAPABILITY],
            "suspension": (
                {
                    "reason": s.reason,
                    "since": s.suspended_at.isoformat(),
                    "actor": s.actor,
                }
                if (s := agg.suspended_at(at)) is not None
                else None
            ),
        }

    def timeline(self, application_id: str) -> dict[str, Any]:
        events = self.store.load_stream(f"app:{application_id}")
        return {
            "application_id": application_id,
            "events": [
                {
                    "seq": e.seq,
                    "event_id": e.event_id,
                    "type": e.event_type,
                    "at": e.at.isoformat(),
                    "actor": e.actor,
                    "version": e.version,
                    "data": e.data,
                }
                for e in events
            ],
        }

    def list_applications(self, state: str | None = None) -> dict[str, Any]:
        now = self.clock.now()
        items = []
        for app_id in self._application_ids():
            view = self.get_application_view(app_id)
            if state and view["state"] != state:
                continue
            items.append({
                "application_id": app_id,
                "fleet_id": view["fleet_id"],
                "jurisdiction": view["jurisdiction"],
                "state": view["state"],
                "current_version": view["current_version"],
                "permit_number": view["permit_number"],
            })
        return {"applications": items, "as_of": now.isoformat()}

    def list_closures(self) -> dict[str, Any]:
        result = []
        for stream_id in self.store.stream_ids("closure:"):
            event = self.store.load_stream(stream_id)[0]
            result.append(event.data)
        return {"closures": sorted(result, key=lambda c: c["starts_at"])}

    def list_conflicts(self, status: str | None = None) -> dict[str, Any]:
        items = []
        for stream_id in self.store.stream_ids("conflict:"):
            cid = stream_id.split(":", 1)[1]
            view = self.get_conflict_view(cid)
            if status and view["status"] != status:
                continue
            items.append(view)
        return {"conflicts": items}

    def get_conflict_view(self, conflict_id: str) -> dict[str, Any]:
        conflict = self._load_conflict(conflict_id)
        return {
            "conflict_id": conflict.conflict_id,
            "status": conflict.status.value,
            "closure_id": conflict.closure_id,
            "application_id": conflict.application_id,
            "segment_code": conflict.segment_code,
            "window_id": conflict.window_id,
            "original_window": conflict.original_window,
            "closure": conflict.closure,
            "opened_at": conflict.opened_at.isoformat() if conflict.opened_at else None,
            "proposals": [
                {
                    "at": p.at.isoformat(),
                    "actor": p.actor,
                    "added_windows": p.added_windows,
                    "removed_window_ids": p.removed_window_ids,
                    "note": p.note,
                }
                for p in conflict.proposals
            ],
            "response": conflict.response,
            "approval": conflict.approval,
            "closed": conflict.closed,
        }

    def list_recognitions(self, as_of: datetime | None = None) -> dict[str, Any]:
        return {
            "recognitions": [
                self.get_recognition_view(sid.split(":", 1)[1], as_of)
                for sid in self.store.stream_ids("recognition:")
            ]
        }

    def get_recognition_view(
        self, recognition_id: str, as_of: datetime | None = None
    ) -> dict[str, Any]:
        events = self.store.load_stream(f"recognition:{recognition_id}")
        if as_of is not None:
            events = [e for e in events if e.at <= as_of]
        rec = agg_mod.rebuild_recognition(events)
        at = as_of or self.clock.now()

        effective: dict[str, Any] = {}
        source_state: str | None = None
        try:
            source = self._load(rec.source_application_id)
            source_state = source.effective_state_at(at).value
            # 源许可暂停/撤销期间，认可范围处于不可执行状态
            active = source.active_grants_at(at) if source_state == PermitState.SIGNED.value else []
            for kind in ("vehicle", "driver", "capability", "route_window"):
                # scope 以 kind.value 为键存储（见 RecognitionGranted 事件）
                granted = set(rec.scope.get(kind, []))
                live = {g.item_id for g in active if g.kind.value == kind}
                effective[kind] = {
                    "recognized_and_effective": sorted(granted & live),
                    "recognized_but_no_longer_effective": sorted(granted - live),
                }
        except NotFoundError:
            source = None  # type: ignore[assignment]

        return {
            "recognition_id": rec.recognition_id,
            "state": rec.state.value if rec.granted_at and (not as_of or rec.granted_at <= at) else "pending",
            "recognizing_jurisdiction": rec.recognizing_jurisdiction,
            "origin_jurisdiction": rec.origin_jurisdiction,
            "source_application_id": rec.source_application_id,
            "granted_at": rec.granted_at.isoformat() if rec.granted_at else None,
            "scope": rec.scope,
            "basis": rec.grant_basis,
            "amendments": rec.amendments,
            "withdrawal": rec.withdrawal,
            "source_permit_state_at": source_state,
            "effective_scope_at": effective,
            "as_of": at.isoformat(),
        }

    # ======================================================================
    # 内部装配
    # ======================================================================

    def _application_ids(self) -> list[str]:
        return [sid.split(":", 1)[1] for sid in self.store.stream_ids("app:")]

    def _conflict_index(self) -> dict[tuple[str, str, str], str]:
        index: dict[tuple[str, str, str], str] = {}
        for stream_id in self.store.stream_ids("conflict:"):
            conflict = agg_mod.rebuild_conflict(self.store.load_stream(stream_id))
            index[(conflict.closure_id, conflict.application_id, conflict.window_id)] = (
                conflict.conflict_id if conflict.status.value not in ("resolved_rescheduled", "resolved_cancelled")
                else conflict.status.value
            )
        return index

    def _load(self, application_id: str) -> agg_mod.PermitAggregate:
        return agg_mod.rebuild(self.store.load_stream(f"app:{application_id}"))

    def _load_conflict(self, conflict_id: str) -> agg_mod.ConflictAggregate:
        return agg_mod.rebuild_conflict(self.store.load_stream(f"conflict:{conflict_id}"))

    def _load_recognition(self, recognition_id: str) -> agg_mod.RecognitionAggregate:
        return agg_mod.rebuild_recognition(self.store.load_stream(f"recognition:{recognition_id}"))

    def _require_reviewable(self, agg: agg_mod.PermitAggregate) -> None:
        if agg.state in (PermitState.REVOKED,):
            raise WorkflowError("许可已撤销")
        if agg.state == PermitState.DRAFT:
            raise WorkflowError("申请尚未提交版本")
        if agg.state == PermitState.SUSPENDED:
            raise WorkflowError("许可暂停中，请先恢复再处理新版本")

    @staticmethod
    def _validate_unique(items: tuple[Any, ...], attr: str) -> None:
        values = [getattr(i, attr) for i in items]
        if len(set(values)) != len(values):
            raise ValidationError(f"{attr} 存在重复", {"values": values})

    @staticmethod
    def _assert_no_internal_window_overlap(windows: tuple[RouteWindow, ...]) -> None:
        for i, left in enumerate(windows):
            for right in windows[i + 1 :]:
                if left.overlaps(right):
                    raise ValidationError(
                        f"同一路段 {left.segment_code} 的时窗不能自相重叠",
                        {
                            "segment_code": left.segment_code,
                            "window_a": window_to_dict(left),
                            "window_b": window_to_dict(right),
                        },
                    )


def _base_of(item_id: str) -> str:
    kind, rest = item_id.split(":", 1)
    if kind == "route":
        # route:<segment>:<window_id>，同一辖区路段的改期视为"变更"
        segment = rest.split(":", 1)[0]
        return f"{kind}:{segment}"
    if "@" in rest:
        return f"{kind}:{rest.split('@', 1)[0]}"
    return item_id


def _change_reason(kind: ApprovalKind) -> str:
    return {
        ApprovalKind.VEHICLE: "车辆资质内容发生变更（号牌/资质版本/有效期/能力等），旧批准失效",
        ApprovalKind.DRIVER: "驾驶员授权内容发生变更，旧批准失效",
        ApprovalKind.CAPABILITY: "测试能力证书发生变更，旧批准失效",
        ApprovalKind.ROUTE_WINDOW: "路线时窗发生变化，旧时窗批准失效",
    }[kind]


def _as_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        raise ValidationError(f"{key} 必须是非空数组")
    if not all(isinstance(item, dict) for item in value):
        raise ValidationError(f"{key} 中每一项必须是对象")
    return value
