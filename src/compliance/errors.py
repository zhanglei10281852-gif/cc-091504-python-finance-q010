"""领域错误与 HTTP 状态码映射。"""

from __future__ import annotations


class ComplianceError(Exception):
    """所有领域错误的基类。"""

    http_status = 400
    code = "invalid_request"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code

    def to_payload(self) -> dict:
        return {"error": {"code": self.code, "message": str(self)}}


class ValidationError(ComplianceError):
    http_status = 400
    code = "invalid_payload"


class NotFoundError(ComplianceError):
    http_status = 404
    code = "not_found"


class PlanLockedError(ComplianceError):
    """送审中的计划被普通编辑触碰，或状态机不允许该动作。"""

    http_status = 409
    code = "plan_locked"


class StateConflictError(ComplianceError):
    http_status = 409
    code = "state_conflict"
