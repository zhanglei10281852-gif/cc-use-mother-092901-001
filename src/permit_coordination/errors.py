"""领域错误类型，HTTP 层据此映射状态码。"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """所有可预期的业务错误基类。"""

    status_code = 400
    code = "domain_error"

    def __init__(self, message: str, details: Any = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.details = details
        if code:
            self.code = code

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, "details": self.details}


class ValidationError(DomainError):
    status_code = 400
    code = "validation_error"


class NotFoundError(DomainError):
    status_code = 404
    code = "not_found"


class ConflictError(DomainError):
    status_code = 409
    code = "conflict"


class WorkflowError(DomainError):
    status_code = 409
    code = "workflow_state_error"


class RuleViolationError(DomainError):
    status_code = 422
    code = "rule_violation"

    def __init__(self, message: str, findings: list[dict[str, Any]]):
        super().__init__(message, findings)
        self.findings = findings

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "message": self.message,
            "details": {"findings": self.findings},
        }


class OccupancyConflictError(DomainError):
    status_code = 409
    code = "occupancy_conflict"

    def __init__(self, message: str, conflicts: list[dict[str, Any]]):
        super().__init__(message, {"conflicts": conflicts})
        self.conflicts = conflicts
