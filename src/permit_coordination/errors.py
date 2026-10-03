"""领域错误，HTTP 层据此映射状态码。"""


class DomainError(Exception):
    """所有可预期的业务校验错误基类。"""

    http_status = 400
    code = "domain_error"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_body(self) -> dict:
        body = {"error": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return body


class NotFoundError(DomainError):
    http_status = 404
    code = "not_found"


class ConflictError(DomainError):
    http_status = 409
    code = "conflict"


class ValidationError(DomainError):
    http_status = 400
    code = "validation_error"
