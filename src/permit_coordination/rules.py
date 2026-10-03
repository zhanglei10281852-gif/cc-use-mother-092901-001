"""辖区审查规则。

每条规则读取一次提交版本的车辆资质、驾驶员授权、测试能力与路线时窗，
输出结构化检查结论（finding）。规则是纯函数，不依赖存储，便于单测与扩展。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

# 辖区代码（演示用的一体化示范区片区：临港、嘉定、金桥等）
JINGHAI = "JINGHAI"
JIADING = "JIADING"
LIN_GANG = "LIN_GANG"

# 允许测试的路段前缀：R-<片区>-<编号>
SEGMENT_JURISDICTION = {
    "JH": JINGHAI,
    "JD": JIADING,
    "LG": LIN_GANG,
}

LEVEL_ORDER = {"L2": 2, "L3": 3, "L4": 4, "L5": 5}


@dataclass(frozen=True)
class RuleContext:
    at: datetime

    @property
    def now(self) -> datetime:
        return self.at


def jurisdiction_for_segment(segment_code: str) -> str | None:
    parts = segment_code.split("-")
    if len(parts) >= 2:
        return SEGMENT_JURISDICTION.get(parts[1].upper())
    return None


def finding(code: str, message: str, severity: str = "blocking") -> dict:
    return {"code": code, "severity": severity, "message": message}


def _cover(value: dict | None, start: datetime, end: datetime) -> bool:
    if not value:
        return False
    valid_from = value.get("valid_from")
    valid_to = value.get("valid_to")
    if valid_from and datetime.fromisoformat(valid_from) > start:
        return False
    if valid_to and datetime.fromisoformat(valid_to) < end:
        return False
    return True


def _window_span(windows) -> tuple[datetime, datetime]:
    return min(w.starts_at for w in windows), max(w.ends_at for w in windows)


def _vehicle_qual(spec: dict, qual_type: str) -> dict | None:
    for qual in spec.get("qualifications", []):
        if qual.get("type") == qual_type:
            return qual
    return None


def _driver_auth(auth: dict, auth_type: str) -> dict | None:
    for item in auth.get("authorizations", []):
        if item.get("type") == auth_type:
            return item
    return None


Rule = Callable[[object, RuleContext], list[dict]]


def rule_vehicle_road_test_qualification(submission, ctx: RuleContext) -> list[dict]:
    start, end = _window_span(submission.route_windows)
    findings = []
    for vehicle_id, spec in submission.vehicle_specs.items():
        qual = _vehicle_qual(spec, "road_test_qualification")
        if not _cover(qual, start, end):
            findings.append(finding(
                "VEHICLE_QUALIFICATION_EXPIRED",
                f"车辆 {vehicle_id} 缺少覆盖测试时窗的道路测试资质"))
    return findings


def rule_vehicle_insurance(submission, ctx: RuleContext) -> list[dict]:
    start, end = _window_span(submission.route_windows)
    findings = []
    for vehicle_id, spec in submission.vehicle_specs.items():
        insurance = spec.get("insurance")
        if not _cover(insurance, start, end):
            findings.append(finding(
                "VEHICLE_INSURANCE_MISSING",
                f"车辆 {vehicle_id} 缺少覆盖测试时窗的交通事故责任保险"))
    return findings


def rule_safety_driver_authorization(submission, ctx: RuleContext) -> list[dict]:
    start, end = _window_span(submission.route_windows)
    findings = []
    for driver_id, auth in submission.driver_authorizations.items():
        granted = _driver_auth(auth, "safety_driver")
        if not _cover(granted, start, end):
            findings.append(finding(
                "DRIVER_AUTHORIZATION_EXPIRED",
                f"驾驶员 {driver_id} 缺少覆盖测试时窗的安全员授权"))
    return findings


def rule_driver_training(submission, ctx: RuleContext) -> list[dict]:
    findings = []
    for driver_id, auth in submission.driver_authorizations.items():
        trained = _driver_auth(auth, "automated_driving_training")
        if not trained:
            findings.append(finding(
                "DRIVER_TRAINING_MISSING",
                f"驾驶员 {driver_id} 缺少自动驾驶培训记录"))
    return findings


def rule_capability_level(min_level: str) -> Rule:
    def rule(submission, ctx: RuleContext) -> list[dict]:
        level = submission.capability.get("level", "")
        if LEVEL_ORDER.get(level, 0) < LEVEL_ORDER[min_level]:
            return [finding(
                "CAPABILITY_LEVEL_INSUFFICIENT",
                f"测试能力等级 {level or '未知'} 低于辖区要求 {min_level}")]
        return []
    return rule


def rule_capability_functions(required: tuple[str, ...]) -> Rule:
    def rule(submission, ctx: RuleContext) -> list[dict]:
        provided = set(submission.capability.get("functions", []))
        missing = [item for item in required if item not in provided]
        if missing:
            return [finding(
                "CAPABILITY_FUNCTION_MISSING",
                f"测试能力缺少声明项: {', '.join(missing)}")]
        return []
    return rule


def rule_technical_compliance(submission, ctx: RuleContext) -> list[dict]:
    certs = set(submission.capability.get("certifications", []))
    if "technical_guidelines_compliance" not in certs:
        return [finding(
            "TECHNICAL_COMPLIANCE_MISSING",
            "缺少智能网联汽车道路测试技术规范符合性声明")]
    return []


def rule_segments_allowed(allowed_prefixes: tuple[str, ...]) -> Rule:
    def rule(submission, ctx: RuleContext) -> list[dict]:
        findings = []
        for window in submission.route_windows:
            if not window.segment_code.startswith(allowed_prefixes):
                findings.append(finding(
                    "SEGMENT_NOT_ALLOWED",
                    f"路段 {window.segment_code} 不在辖区开放测试道路目录内"))
        return findings
    return rule


# 辖区规则表：审查时按提交版本的辖区取规则
JURISDICTION_RULES: dict[str, tuple[Rule, ...]] = {
    JINGHAI: (
        rule_vehicle_road_test_qualification,
        rule_vehicle_insurance,
        rule_safety_driver_authorization,
        rule_driver_training,
        rule_capability_level("L3"),
        rule_capability_functions(("emergency_stop", "remote_monitoring")),
        rule_segments_allowed(("R-JH-",)),
    ),
    JIADING: (
        rule_vehicle_road_test_qualification,
        rule_vehicle_insurance,
        rule_safety_driver_authorization,
        rule_capability_level("L4"),
        rule_capability_functions(("emergency_stop", "remote_monitoring", "data_recording")),
        rule_segments_allowed(("R-JD-",)),
    ),
    LIN_GANG: (
        rule_vehicle_road_test_qualification,
        rule_vehicle_insurance,
        rule_safety_driver_authorization,
        rule_driver_training,
        rule_capability_level("L4"),
        rule_technical_compliance,
        rule_segments_allowed(("R-LG-",)),
    ),
}

DEFAULT_RULES: tuple[Rule, ...] = (
    rule_vehicle_road_test_qualification,
    rule_safety_driver_authorization,
    rule_capability_level("L3"),
)


def rules_for(jurisdiction: str) -> tuple[Rule, ...]:
    return JURISDICTION_RULES.get(jurisdiction, DEFAULT_RULES)


def evaluate(submission, at: datetime) -> list[dict]:
    """对一份提交版本执行辖区全部规则，返回检查结论。"""
    ctx = RuleContext(at=at)
    findings: list[dict] = []
    for rule in rules_for(submission.jurisdiction):
        findings.extend(rule(submission, ctx))
    return findings


def involved_jurisdictions(windows, home_jurisdiction: str) -> list[str]:
    """根据路线时窗推断涉及的辖区；归属不明的路段归入申请辖区。"""
    result = []
    for window in windows:
        owner = jurisdiction_for_segment(window.segment_code) or home_jurisdiction
        if owner not in result:
            result.append(owner)
    if home_jurisdiction not in result:
        result.append(home_jurisdiction)
    return result
