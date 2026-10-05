"""可区分的业务错误（骨架）。"""
from __future__ import annotations


class ControlPlaneError(Exception):
    """业务错误基类，code 用于客户端区分原因。"""

    code = "internal_error"
    http_status = 500

    def __init__(self, message: str = ""):
        super().__init__(message)
        self.message = message


class NotFoundError(ControlPlaneError):
    code = "not_found"
    http_status = 404


class ForbiddenError(ControlPlaneError):
    code = "forbidden"
    http_status = 403


class InvalidTransitionError(ControlPlaneError):
    code = "invalid_transition"
    http_status = 409


class AlreadyReclaimedError(ControlPlaneError):
    code = "already_reclaimed"
    http_status = 409


class QuotaExceededError(ControlPlaneError):
    code = "quota_exceeded"
    http_status = 409


class UnsettledEnvironmentsError(ControlPlaneError):
    code = "unsettled_environments"
    http_status = 409
