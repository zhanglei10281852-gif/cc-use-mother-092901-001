"""应用服务：把 HTTP 命令翻译为领域事件，承载全部业务校验。

所有写操作满足：
- 基于重放后的投影做判定，再追加事件（命令级加锁保证读-判-写原子）；
- 重复提交幂等，不产生多份有效许可；
- 车辆/路线新版签发时，明确作废旧许可；
- 封路自动产生冲突单，改期/取消结果回写许可有效时窗。
"""

import threading
import uuid
from datetime import datetime

from permit_coordination.clock import Clock, SystemClock
from permit_coordination.contracts import RouteWindow
from permit_coordination.domain import (
    Projection,
    content_hash,
    windows_overlap,
)
from permit_coordination.errors import ConflictError, NotFoundError, ValidationError
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
from permit_coordination.rules import evaluate, involved_jurisdictions
from permit_coordination.store import EventStore

# 占用道路的状态：仅“已签发”；暂停的许可不占用路段，也不可被互认
OCCUPYING_STATUSES = ("issued",)
# 仍可被撤销/换发作废的生命周期状态
LIFECYCLE_OPEN_STATUSES = ("issued", "suspended")


def parse_window(data: dict) -> RouteWindow:
    try:
        return RouteWindow(
            segment_code=data["segment_code"],
            starts_at=datetime.fromisoformat(data["starts_at"]),
            ends_at=datetime.fromisoformat(data["ends_at"]),
        )
    except KeyError as exc:
        raise ValidationError(f"路线时窗缺少字段: {exc.args[0]}")
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"路线时窗无效: {exc}")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class PermitService:
    def __init__(self, store: EventStore, clock: Clock | None = None) -> None:
        self.store = store
        self.clock = clock or SystemClock()
        self._lock = threading.RLock()
        self.projection = Projection.replay(store.all())
        self._idempotency: dict[str, str] = {
            s.idempotency_key: s.submission_id
            for s in self.projection.submissions.values()
            if s.idempotency_key
        }
        store.add_listener(self.projection.apply)

    # ------------------------------------------------------------------ 内部

    def _append(self, event: Event) -> Event:
        self.store.append(event)
        return event

    def _get_permit(self, permit_id: str):
        permit = self.projection.permits.get(permit_id)
        if permit is None:
            raise NotFoundError(f"许可不存在: {permit_id}")
        return permit

    def _get_review(self, review_id: str):
        review = self.projection.reviews.get(review_id)
        if review is None:
            raise NotFoundError(f"审查单不存在: {review_id}")
        return review

    def _latest_active_submission(self, application_id: str):
        actives = self.projection.active_submissions(application_id)
        return actives[-1] if actives else None

    # ------------------------------------------------------------ 申请提交

    def submit_application(self, payload: dict, *, actor: str) -> dict:
        """提交（或修订）一个申请版本。

        载荷内容与上一有效版本完全一致时记为重复提交，不新增有效版本。
        """
        application_id = payload.get("application_id")
        if not application_id:
            raise ValidationError("缺少 application_id")
        jurisdiction = payload.get("jurisdiction")
        if not jurisdiction:
            raise ValidationError("缺少辖区代码 jurisdiction")
        fleet_id = payload.get("fleet_id")
        if not fleet_id:
            raise ValidationError("缺少车队标识 fleet_id")

        vehicle_specs = payload.get("vehicles")
        driver_authorizations = payload.get("drivers")
        capability = payload.get("capability")
        raw_windows = payload.get("route_windows")
        if not isinstance(vehicle_specs, dict) or not vehicle_specs:
            raise ValidationError("vehicles 必须为非空车辆资质映射")
        if not isinstance(driver_authorizations, dict) or not driver_authorizations:
            raise ValidationError("drivers 必须为非空驾驶员授权映射")
        if not isinstance(capability, dict):
            raise ValidationError("capability 必须为测试能力声明对象")
        if not isinstance(raw_windows, list) or not raw_windows:
            raise ValidationError("route_windows 必须为非空路线时窗列表")
        windows = tuple(parse_window(item) for item in raw_windows)

        idempotency_key = payload.get("idempotency_key")
        now = self.clock.now()

        with self._lock:
            if idempotency_key and idempotency_key in self._idempotency:
                prior_id = self._idempotency[idempotency_key]
                return {"submission_id": prior_id, "duplicate": True,
                        "reason": "idempotency_key"}

            previous = self._latest_active_submission(application_id)
            digest = content_hash(vehicle_specs, driver_authorizations, capability, windows)

            duplicate_of = None
            supersedes = None
            changed_vehicles: tuple[str, ...] = ()
            changed_segments: tuple[str, ...] = ()
            if previous is not None:
                if previous.content_hash == digest:
                    duplicate_of = previous.submission_id
                else:
                    version = previous.version + 1
                    supersedes = previous.submission_id
                    changed_vehicles = self._diff_vehicles(previous, vehicle_specs)
                    changed_segments = self._diff_segments(previous, windows)
            else:
                version = 1

            if duplicate_of:
                # 重复提交仍留痕，但不产生新版本、不影响既有许可
                version = previous.version
                submission_id = _new_id("SUB")
            else:
                submission_id = _new_id("SUB")

            event = ApplicationSubmitted(
                at=now, actor=actor, application_id=application_id, version=version,
                fleet_id=fleet_id, jurisdiction=jurisdiction, submission_id=submission_id,
                vehicle_specs=vehicle_specs, driver_authorizations=driver_authorizations,
                capability=capability, route_windows=windows,
                supersedes_submission_id=supersedes,
                superseded_permit_ids=(),
                changed_vehicles=changed_vehicles, changed_segments=changed_segments,
                duplicate_of_submission_id=duplicate_of, idempotency_key=idempotency_key,
            )
            self._append(event)
            if idempotency_key:
                self._idempotency[idempotency_key] = submission_id
            return {"submission_id": submission_id, "duplicate": bool(duplicate_of),
                    "version": version,
                    "duplicate_of": duplicate_of,
                    "changed_vehicles": list(changed_vehicles),
                    "changed_segments": list(changed_segments)}

    @staticmethod
    def _diff_vehicles(previous, new_specs: dict) -> tuple[str, ...]:
        changed = []
        old_ids = set(previous.vehicle_specs)
        new_ids = set(new_specs)
        for vehicle_id in sorted(old_ids | new_ids):
            if vehicle_id not in old_ids or vehicle_id not in new_ids:
                changed.append(vehicle_id)
            elif previous.vehicle_specs[vehicle_id] != new_specs[vehicle_id]:
                changed.append(vehicle_id)
        return tuple(changed)

    @staticmethod
    def _diff_segments(previous, new_windows) -> tuple[str, ...]:
        old = {(w.segment_code, w.starts_at, w.ends_at) for w in previous.route_windows}
        new = {(w.segment_code, w.starts_at, w.ends_at) for w in new_windows}
        old_segments = {item[0] for item in old}
        new_segments = {item[0] for item in new}
        changed = {seg for seg in old_segments ^ new_segments}
        # 同路段时窗调整也算路段变更
        for seg, start, end in old - new:
            if seg in new_segments:
                changed.add(seg)
        return tuple(sorted(changed))

    # ------------------------------------------------------------ 审查补件

    def open_review(self, application_id: str, version: int, jurisdiction: str,
                    *, actor: str) -> dict:
        with self._lock:
            submission = self._require_version(application_id, version)
            existing = self.projection.reviews_by_version.get(
                (application_id, version), {}).get(jurisdiction)
            if existing:
                return {"review_id": existing, "duplicate": True}

            review_id = _new_id("REV")
            self._append(ReviewOpened(
                at=self.clock.now(), actor=actor, application_id=application_id,
                version=version, review_id=review_id, jurisdiction=jurisdiction))

            # 按辖区规则自动审查；存在阻断项时直接进入补件状态
            findings = evaluate(submission, self.clock.now())
            blocking = [item for item in findings if item.get("severity", "blocking") == "blocking"]
            if blocking:
                requested = sorted({item["code"] for item in blocking})
                self._append(SupplementRequested(
                    at=self.clock.now(), actor=actor, application_id=application_id,
                    version=version, review_id=review_id, jurisdiction=jurisdiction,
                    findings=tuple(blocking), requested_items=tuple(requested)))
            return {"review_id": review_id, "duplicate": False,
                    "findings": findings}

    def request_supplement(self, review_id: str, findings: list[dict],
                           requested_items: list[str], *, actor: str) -> dict:
        with self._lock:
            review = self._get_review(review_id)
            if review.state in ("approved", "rejected"):
                raise ConflictError(f"审查已{review.state}，不能要求补件")
            normalized = []
            for item in findings:
                if "code" not in item:
                    raise ValidationError("finding 必须包含 code")
                normalized.append({
                    "code": item["code"],
                    "severity": item.get("severity", "blocking"),
                    "message": item.get("message", ""),
                })
            self._append(SupplementRequested(
                at=self.clock.now(), actor=actor, application_id=review.application_id,
                version=review.version, review_id=review_id,
                jurisdiction=review.jurisdiction, findings=tuple(normalized),
                requested_items=tuple(requested_items)))
            return {"review_id": review_id, "state": "supplement_requested"}

    def submit_supplement(self, review_id: str, documents: list[dict],
                          resolved_finding_codes: list[str], *, actor: str) -> dict:
        with self._lock:
            review = self._get_review(review_id)
            if review.state in ("approved", "rejected"):
                raise ConflictError(f"审查已{review.state}，不能补件")
            if not documents:
                raise ValidationError("补件必须包含至少一份材料")
            open_blocking = {f.code for f in review.findings
                             if not f.resolved and f.severity == "blocking"}
            unknown = set(resolved_finding_codes) - {f.code for f in review.findings}
            if unknown:
                raise ValidationError(
                    f"补件试图解决不存在的检查项: {sorted(unknown)}",
                    details={"open_findings": sorted(open_blocking)})
            self._append(SupplementSubmitted(
                at=self.clock.now(), actor=actor, application_id=review.application_id,
                version=review.version, review_id=review_id,
                documents=tuple(documents),
                resolved_finding_codes=tuple(resolved_finding_codes)))
            review = self.projection.reviews[review_id]
            return {"review_id": review_id, "state": review.state,
                    "open_blocking_findings": sorted(
                        f.code for f in review.findings
                        if not f.resolved and f.severity == "blocking")}

    def approve_review(self, review_id: str, conditions: list[str] | None = None,
                       *, actor: str) -> dict:
        with self._lock:
            review = self._get_review(review_id)
            if review.state == "approved":
                return {"review_id": review_id, "duplicate": True}
            if review.state == "rejected":
                raise ConflictError("审查已驳回，不能批准")
            unresolved = [f.code for f in review.findings
                          if not f.resolved and f.severity == "blocking"]
            if unresolved:
                raise ConflictError(
                    "仍有阻断性检查项未补件", details={"open_findings": unresolved})
            self._append(ReviewApproved(
                at=self.clock.now(), actor=actor, application_id=review.application_id,
                version=review.version, review_id=review_id,
                jurisdiction=review.jurisdiction, conditions=tuple(conditions or ())))
            return {"review_id": review_id, "state": "approved"}

    def reject_review(self, review_id: str, reason: str, *, actor: str) -> dict:
        with self._lock:
            review = self._get_review(review_id)
            if review.state in ("approved", "rejected"):
                raise ConflictError(f"审查已{review.state}，不能驳回")
            self._append(ReviewRejected(
                at=self.clock.now(), actor=actor, application_id=review.application_id,
                version=review.version, review_id=review_id,
                jurisdiction=review.jurisdiction, reason=reason))
            return {"review_id": review_id, "state": "rejected"}

    def _require_version(self, application_id: str, version: int):
        ids = self.projection.application_versions.get(application_id)
        if not ids:
            raise NotFoundError(f"申请不存在: {application_id}")
        for submission_id in reversed(ids):
            submission = self.projection.submissions[submission_id]
            if submission.version == version and submission.duplicate_of is None:
                return submission
        raise NotFoundError(f"申请 {application_id} 不存在版本 {version}")

    # ------------------------------------------------------------ 会签签发

    def issue_permit(self, application_id: str, version: int, *, actor: str,
                     permit_id: str | None = None) -> dict:
        with self._lock:
            submission = self._require_version(application_id, version)
            latest = self._latest_active_submission(application_id)
            if latest is None or latest.submission_id != submission.submission_id:
                raise ConflictError(
                    "只能就最新有效版本签发许可",
                    details={"latest_version": latest.version if latest else None})

            required = involved_jurisdictions(submission.route_windows, submission.jurisdiction)
            basis = []
            missing = []
            for jurisdiction in required:
                review_id = self.projection.reviews_by_version.get(
                    (application_id, version), {}).get(jurisdiction)
                review = self.projection.reviews.get(review_id) if review_id else None
                if review is None:
                    missing.append(jurisdiction)
                elif review.state != "approved":
                    missing.append(jurisdiction)
                else:
                    basis.append({
                        "jurisdiction": jurisdiction,
                        "review_id": review.review_id,
                        "decided_by": review.decided_by,
                        "decided_at": review.decided_at.isoformat(),
                        "conditions": list(review.conditions),
                    })
            if missing:
                raise ConflictError(
                    "会签辖区不完整，不能签发",
                    details={"missing_jurisdictions": missing,
                             "required_jurisdictions": required})

            conflicts = self._scheduling_conflicts(submission.route_windows,
                                                    ignore_application=application_id)
            if conflicts:
                raise ConflictError("路线时窗存在占用或封路冲突", details={"conflicts": conflicts})

            permit_id = permit_id or _new_id("PMT")
            if permit_id in self.projection.permits:
                raise ConflictError(f"许可编号已存在: {permit_id}")

            windows = submission.route_windows
            validity_start = min(w.starts_at for w in windows)
            validity_end = max(w.ends_at for w in windows)
            self._append(PermitIssued(
                at=self.clock.now(), actor=actor, permit_id=permit_id,
                application_id=application_id, version=version,
                fleet_id=submission.fleet_id,
                issuing_jurisdiction=submission.jurisdiction,
                approving_jurisdictions=tuple(required),
                vehicle_ids=tuple(sorted(submission.vehicle_specs)),
                driver_ids=tuple(sorted(submission.driver_authorizations)),
                route_windows=windows, review_basis=tuple(basis),
                validity_start=validity_start, validity_end=validity_end))

            # 同一申请此前仍有效的许可随新版签发明示作废
            superseded = []
            for old_id in self.projection.permit_ids_by_application.get(application_id, []):
                if old_id == permit_id:
                    continue
                old = self.projection.permits[old_id]
                if old.status in LIFECYCLE_OPEN_STATUSES:
                    self._append(PermitSuperseded(
                        at=self.clock.now(), actor=actor, permit_id=old_id,
                        replaced_by_permit_id=permit_id,
                        changed_vehicles=submission.changed_vehicles,
                        changed_segments=submission.changed_segments,
                        reason="申请方提交修订版本并换发新许可"))
                    superseded.append(old_id)
            return {"permit_id": permit_id, "superseded_permit_ids": superseded,
                    "approving_jurisdictions": required}

    def _scheduling_conflicts(self, windows, *, ignore_application: str | None = None,
                              ignore_permit: str | None = None) -> list[dict]:
        result = []
        for window in windows:
            for closure in self.projection.closures.values():
                if window.segment_code in closure.segment_codes and windows_overlap(
                        window.starts_at, window.ends_at, closure.starts_at, closure.ends_at):
                    result.append({
                        "type": "road_closure", "segment_code": window.segment_code,
                        "window": [window.starts_at.isoformat(), window.ends_at.isoformat()],
                        "closure_id": closure.closure_id,
                        "closure": [closure.starts_at.isoformat(), closure.ends_at.isoformat()],
                    })
            for permit in self.projection.permits.values():
                if permit.status not in OCCUPYING_STATUSES:
                    continue
                if ignore_permit and permit.permit_id == ignore_permit:
                    continue
                if ignore_application and permit.application_id == ignore_application:
                    continue
                for other in permit.effective_windows():
                    if other.segment_code == window.segment_code and windows_overlap(
                            window.starts_at, window.ends_at, other.starts_at, other.ends_at):
                        result.append({
                            "type": "permit_overlap", "segment_code": window.segment_code,
                            "window": [window.starts_at.isoformat(), window.ends_at.isoformat()],
                            "permit_id": permit.permit_id,
                            "other_window": [other.starts_at.isoformat(), other.ends_at.isoformat()],
                        })
        return result

    # ------------------------------------------------------------ 暂停撤销

    def suspend_permit(self, permit_id: str, reason: str, *, actor: str) -> dict:
        with self._lock:
            permit = self._get_permit(permit_id)
            if permit.status == "suspended":
                return {"permit_id": permit_id, "duplicate": True}
            if permit.status != "issued":
                raise ConflictError(f"许可状态为 {permit.status}，不能暂停")
            if not reason:
                raise ValidationError("暂停必须给出原因")
            self._append(PermitSuspended(at=self.clock.now(), actor=actor,
                                         permit_id=permit_id, reason=reason))
            return {"permit_id": permit_id, "status": "suspended"}

    def reinstate_permit(self, permit_id: str, *, actor: str) -> dict:
        with self._lock:
            permit = self._get_permit(permit_id)
            if permit.status == "issued":
                return {"permit_id": permit_id, "duplicate": True}
            if permit.status != "suspended":
                raise ConflictError(f"许可状态为 {permit.status}，不能恢复")
            # 暂停期间路段可能已被封路或批给其他车队，恢复前重新检测
            blockers = self._scheduling_conflicts(
                permit.effective_windows(), ignore_permit=permit_id)
            if blockers:
                raise ConflictError("暂停期间路段安排已变化，不能直接恢复",
                                    details={"conflicts": blockers})
            self._append(PermitReinstated(at=self.clock.now(), actor=actor,
                                          permit_id=permit_id))
            return {"permit_id": permit_id, "status": "issued"}

    def revoke_permit(self, permit_id: str, reason: str, *, actor: str) -> dict:
        with self._lock:
            permit = self._get_permit(permit_id)
            if permit.status == "revoked":
                return {"permit_id": permit_id, "duplicate": True}
            if permit.status not in LIFECYCLE_OPEN_STATUSES:
                raise ConflictError(f"许可状态为 {permit.status}，不能撤销")
            if not reason:
                raise ValidationError("撤销必须给出原因")
            self._append(PermitRevoked(at=self.clock.now(), actor=actor,
                                       permit_id=permit_id, reason=reason))
            return {"permit_id": permit_id, "status": "revoked"}

    # ------------------------------------------------------------ 跨区互认

    def record_recognition(self, permit_id: str, recognizing_jurisdiction: str,
                           scope: dict, basis: str, *, actor: str) -> dict:
        with self._lock:
            permit = self._get_permit(permit_id)
            if permit.status not in OCCUPYING_STATUSES:
                raise ConflictError(f"许可状态为 {permit.status}，不能被互认")
            if not basis:
                raise ValidationError("互认必须给出依据 basis")
            if recognizing_jurisdiction == permit.issuing_jurisdiction:
                raise ValidationError("发证辖区无需对自身许可做互认")

            vehicles = tuple(scope.get("vehicle_ids", permit.vehicle_ids))
            drivers = tuple(scope.get("driver_ids", permit.driver_ids))
            allowed_segments = {w.segment_code for w in permit.effective_windows()}
            segments_raw = scope.get("segment_codes", sorted(allowed_segments))
            unknown = sorted(set(segments_raw) - allowed_segments)
            if unknown:
                raise ValidationError("互认路段超出原许可范围", details={"segments": unknown})
            extra_v = sorted(set(vehicles) - set(permit.vehicle_ids))
            extra_d = sorted(set(drivers) - set(permit.driver_ids))
            if extra_v or extra_d:
                raise ValidationError("互认车辆/驾驶员超出原许可范围",
                                      details={"vehicles": extra_v, "drivers": extra_d})

            # 互认时窗以改期/取消后的实际安排为界，不能超出原许可有效期
            validity_start = permit.effective_validity_start
            validity_end = permit.effective_validity_end
            window_start = validity_start
            window_end = validity_end
            if scope.get("window_start") or scope.get("window_end"):
                window_start = datetime.fromisoformat(
                    scope.get("window_start", validity_start.isoformat()))
                window_end = datetime.fromisoformat(
                    scope.get("window_end", validity_end.isoformat()))
                if window_start < validity_start or window_end > validity_end:
                    raise ValidationError("互认时窗超出许可实际有效安排（含改期）")
                if window_end <= window_start:
                    raise ValidationError("互认时窗结束必须晚于开始")

            recognition_id = _new_id("REC")
            self._append(RecognitionRecorded(
                at=self.clock.now(), actor=actor, recognition_id=recognition_id,
                permit_id=permit_id, recognizing_jurisdiction=recognizing_jurisdiction,
                vehicle_ids=vehicles, driver_ids=drivers,
                segment_codes=tuple(segments_raw),
                window_start=window_start, window_end=window_end, basis=basis))
            return {"recognition_id": recognition_id}

    def revoke_recognition(self, recognition_id: str, reason: str, *, actor: str) -> dict:
        with self._lock:
            recognition = self.projection.recognitions.get(recognition_id)
            if recognition is None:
                raise NotFoundError(f"互认记录不存在: {recognition_id}")
            if recognition.state != "active":
                raise ConflictError(f"互认记录状态为 {recognition.state}，不能撤销")
            self._append(RecognitionRevoked(
                at=self.clock.now(), actor=actor, recognition_id=recognition_id,
                permit_id=recognition.permit_id, reason=reason))
            return {"recognition_id": recognition_id, "state": "revoked"}

    # ------------------------------------------------------------ 封路冲突

    def register_road_closure(self, segment_codes: list[str], starts_at: str,
                              ends_at: str, reason: str, *, actor: str) -> dict:
        with self._lock:
            start = datetime.fromisoformat(starts_at)
            end = datetime.fromisoformat(ends_at)
            if end <= start:
                raise ValidationError("封路结束时间必须晚于开始时间")
            if not segment_codes:
                raise ValidationError("封路必须包含路段")
            closure_id = _new_id("CLS")
            self._append(RoadClosureRegistered(
                at=self.clock.now(), actor=actor, closure_id=closure_id,
                segment_codes=tuple(segment_codes), starts_at=start, ends_at=end,
                reason=reason or ""))

            conflicts = []
            for permit in self.projection.permits.values():
                if permit.status not in OCCUPYING_STATUSES:
                    continue
                for window in permit.effective_windows():
                    if window.segment_code not in segment_codes:
                        continue
                    if not windows_overlap(window.starts_at, window.ends_at, start, end):
                        continue
                    if self._conflict_exists(closure_id, permit.permit_id, window):
                        continue
                    conflict_id = _new_id("CFL")
                    self._append(ConflictDetected(
                        at=self.clock.now(), actor=actor, conflict_id=conflict_id,
                        closure_id=closure_id, permit_id=permit.permit_id,
                        segment_code=window.segment_code,
                        window_start=window.starts_at, window_end=window.ends_at,
                        closure_start=start, closure_end=end))
                    conflicts.append(conflict_id)
            return {"closure_id": closure_id, "conflict_ids": conflicts}

    def _conflict_exists(self, closure_id: str, permit_id: str, window) -> bool:
        for conflict in self.projection.conflicts.values():
            if (conflict.closure_id == closure_id and conflict.permit_id == permit_id
                    and conflict.segment_code == window.segment_code
                    and conflict.window_start == window.starts_at
                    and conflict.window_end == window.ends_at):
                return True
        return False

    def resolve_conflict(self, conflict_id: str, resolution: str, *, actor: str,
                         new_window: dict | None = None, note: str = "") -> dict:
        with self._lock:
            conflict = self.projection.conflicts.get(conflict_id)
            if conflict is None:
                raise NotFoundError(f"冲突单不存在: {conflict_id}")
            if conflict.state != "open":
                raise ConflictError(f"冲突已{conflict.state}，不能重复处理")
            if resolution not in ("rescheduled", "cancelled"):
                raise ValidationError("resolution 必须为 rescheduled 或 cancelled")

            resolved_window = None
            if resolution == "rescheduled":
                if not new_window:
                    raise ValidationError("改期必须提供新时窗 new_window")
                resolved_window = parse_window(new_window)
                if resolved_window.segment_code != conflict.segment_code:
                    raise ValidationError("改期只能调整时间，不能变更路段（变更路段须重新申请）")
                if resolved_window.starts_at < self.clock.now():
                    raise ValidationError("改期后的开始时间不能早于当前时间")
                blockers = self._scheduling_conflicts(
                    (resolved_window,), ignore_permit=conflict.permit_id)
                if blockers:
                    raise ConflictError("改期新时窗仍有冲突", details={"conflicts": blockers})

            self._append(ConflictResolved(
                at=self.clock.now(), actor=actor, conflict_id=conflict_id,
                resolution=resolution, new_window=resolved_window, note=note or ""))
            return {"conflict_id": conflict_id, "state": resolution}

    # ------------------------------------------------------------ 查询追溯

    def effective_permits(self, at: datetime | None = None) -> list[dict]:
        at = at or self.clock.now()
        return [self._permit_summary(p, at) for p in self.projection.effective_permits(at)]

    def history(self, application_id: str) -> dict:
        ids = self.projection.application_versions.get(application_id)
        if not ids:
            raise NotFoundError(f"申请不存在: {application_id}")
        submissions = [self.projection.submissions[s] for s in ids]
        permit_ids = self.projection.permit_ids_by_application.get(application_id, [])
        return {
            "application_id": application_id,
            "submissions": [
                {
                    "submission_id": s.submission_id, "version": s.version,
                    "at": s.at.isoformat(), "actor": s.actor,
                    "jurisdiction": s.jurisdiction,
                    "duplicate_of": s.duplicate_of,
                    "supersedes_submission_id": s.supersedes_submission_id,
                    "superseded_by": s.superseded_by,
                    "changed_vehicles": list(s.changed_vehicles),
                    "changed_segments": list(s.changed_segments),
                    "content_hash": s.content_hash,
                }
                for s in submissions
            ],
            "permits": [self.permit_detail(pid) for pid in permit_ids],
        }

    def _permit_summary(self, permit, at: datetime | None = None) -> dict:
        recognitions = [
            r for r in self.projection.recognitions.values()
            if r.permit_id == permit.permit_id and r.state == "active"
            and (at is None or (r.recorded_at <= at and (r.window_start <= at < r.window_end)))
        ]
        return {
            "permit_id": permit.permit_id,
            "application_id": permit.application_id,
            "version": permit.version,
            "fleet_id": permit.fleet_id,
            "status": permit.status_at(at) if at else permit.status,
            "issuing_jurisdiction": permit.issuing_jurisdiction,
            "approving_jurisdictions": list(permit.approving_jurisdictions),
            "vehicle_ids": list(permit.vehicle_ids),
            "driver_ids": list(permit.driver_ids),
            "validity_start": permit.validity_start.isoformat(),
            "validity_end": permit.validity_end.isoformat(),
            "issued_at": permit.issued_at.isoformat(),
            "issued_by": permit.issued_by,
            "recognitions": [
                {"recognition_id": r.recognition_id,
                 "jurisdiction": r.recognizing_jurisdiction,
                 "segment_codes": list(r.segment_codes),
                 "vehicle_ids": list(r.vehicle_ids),
                 "basis": r.basis}
                for r in recognitions
            ],
            "effective_windows": [
                {"segment_code": w.segment_code,
                 "starts_at": w.starts_at.isoformat(),
                 "ends_at": w.ends_at.isoformat()}
                for w in permit.effective_windows()
            ],
        }

    def permit_detail(self, permit_id: str) -> dict:
        permit = self._get_permit(permit_id)
        data = self._permit_summary(permit)
        data["status"] = permit.status
        data["review_basis"] = list(permit.review_basis)
        data["status_changes"] = [
            {"at": c.at.isoformat(), "actor": c.actor,
             "from": c.from_status, "to": c.to_status, "reason": c.reason}
            for c in permit.changes
        ]
        if permit.replaced_by_permit_id:
            data["replaced_by_permit_id"] = permit.replaced_by_permit_id
            data["supersede_reason"] = permit.supersede_reason
            data["changed_vehicles"] = list(permit.changed_vehicles)
            data["changed_segments"] = list(permit.changed_segments)
        data["conflicts"] = [
            self.conflict_detail(cid)
            for cid in self.projection.conflicts_by_permit.get(permit_id, [])
        ]
        data["recognitions"] = [
            self.recognition_detail(r.recognition_id)
            for r in self.projection.recognitions.values()
            if r.permit_id == permit_id
        ]
        return data

    def review_detail(self, review_id: str) -> dict:
        review = self._get_review(review_id)
        return {
            "review_id": review.review_id,
            "application_id": review.application_id,
            "version": review.version,
            "jurisdiction": review.jurisdiction,
            "state": review.state,
            "opened_at": review.opened_at.isoformat(),
            "opened_by": review.opened_by,
            "findings": [
                {"code": f.code, "severity": f.severity, "message": f.message,
                 "resolved": f.resolved,
                 "resolved_at": f.resolved_at.isoformat() if f.resolved_at else None,
                 "resolved_by": f.resolved_by}
                for f in review.findings
            ],
            "requested_items": list(review.requested_items),
            "documents": list(review.documents),
            "conditions": list(review.conditions),
            "reason": review.reason or None,
            "decided_at": review.decided_at.isoformat() if review.decided_at else None,
            "decided_by": review.decided_by,
        }

    def recognition_detail(self, recognition_id: str) -> dict:
        r = self.projection.recognitions.get(recognition_id)
        if r is None:
            raise NotFoundError(f"互认记录不存在: {recognition_id}")
        return {
            "recognition_id": r.recognition_id,
            "permit_id": r.permit_id,
            "recognizing_jurisdiction": r.recognizing_jurisdiction,
            "vehicle_ids": list(r.vehicle_ids),
            "driver_ids": list(r.driver_ids),
            "segment_codes": list(r.segment_codes),
            "window_start": r.window_start.isoformat(),
            "window_end": r.window_end.isoformat(),
            "basis": r.basis,
            "state": r.state,
            "recorded_at": r.recorded_at.isoformat(),
            "recorded_by": r.recorded_by,
            "revoked_reason": r.revoked_reason or None,
        }

    def conflict_detail(self, conflict_id: str) -> dict:
        c = self.projection.conflicts.get(conflict_id)
        if c is None:
            raise NotFoundError(f"冲突单不存在: {conflict_id}")
        return {
            "conflict_id": c.conflict_id,
            "closure_id": c.closure_id,
            "permit_id": c.permit_id,
            "segment_code": c.segment_code,
            "window": {"starts_at": c.window_start.isoformat(),
                      "ends_at": c.window_end.isoformat()},
            "closure": {"starts_at": c.closure_start.isoformat(),
                        "ends_at": c.closure_end.isoformat()},
            "state": c.state,
            "new_window": (
                {"segment_code": c.new_window.segment_code,
                 "starts_at": c.new_window.starts_at.isoformat(),
                 "ends_at": c.new_window.ends_at.isoformat()}
                if c.new_window else None),
            "note": c.resolution_note,
            "detected_at": c.detected_at.isoformat(),
            "detected_by": c.detected_by,
            "resolved_at": c.resolved_at.isoformat() if c.resolved_at else None,
            "resolved_by": c.resolved_by,
        }

    def list_conflicts(self, *, state: str | None = None) -> list[dict]:
        items = self.projection.conflicts.values()
        if state:
            items = [c for c in items if c.state == state]
        return [self.conflict_detail(c.conflict_id) for c in items]

    def snapshot_at(self, at: datetime) -> dict:
        """还原指定时点的全部有效许可、责任人与审批依据。"""
        projection = Projection.replay(self.store.all(), upto=at)
        recognitions = [r for r in projection.recognitions.values()
                        if r.state == "active" and r.recorded_at <= at
                        and r.window_start <= at < r.window_end]
        result = []
        for permit in projection.effective_permits(at):
            result.append({
                "permit_id": permit.permit_id,
                "application_id": permit.application_id,
                "version": permit.version,
                "fleet_id": permit.fleet_id,
                "status": permit.status_at(at),
                "issuing_jurisdiction": permit.issuing_jurisdiction,
                "approving_jurisdictions": list(permit.approving_jurisdictions),
                "vehicle_ids": list(permit.vehicle_ids),
                "driver_ids": list(permit.driver_ids),
                "issued_by": permit.issued_by,
                "issued_at": permit.issued_at.isoformat(),
                "review_basis": list(permit.review_basis),
                "route_windows": [
                    {"segment_code": w.segment_code,
                     "starts_at": w.starts_at.isoformat(),
                     "ends_at": w.ends_at.isoformat()}
                    for w in permit.effective_windows()
                ],
                "recognitions": [
                    {"recognition_id": r.recognition_id,
                     "jurisdiction": r.recognizing_jurisdiction,
                     "segment_codes": list(r.segment_codes),
                     "vehicle_ids": list(r.vehicle_ids),
                     "basis": r.basis}
                    for r in recognitions if r.permit_id == permit.permit_id
                ],
            })
        conflicts_at = []
        for conflict in projection.conflicts.values():
            if conflict.detected_at > at:
                continue
            conflicts_at.append({
                "conflict_id": conflict.conflict_id,
                "closure_id": conflict.closure_id,
                "permit_id": conflict.permit_id,
                "segment_code": conflict.segment_code,
                "state": conflict.state,
                "detected_by": conflict.detected_by,
                "resolved_by": conflict.resolved_by,
                "new_window": (
                    {"segment_code": conflict.new_window.segment_code,
                     "starts_at": conflict.new_window.starts_at.isoformat(),
                     "ends_at": conflict.new_window.ends_at.isoformat()}
                    if conflict.new_window else None),
            })
        return {"as_of": at.isoformat(), "effective_permits": result,
                "conflicts": conflicts_at,
                "event_count": sum(projection.event_count_by_type.values())}

    def event_journal(self) -> list[dict]:
        return [e.to_dict() for e in self.store.all()]
