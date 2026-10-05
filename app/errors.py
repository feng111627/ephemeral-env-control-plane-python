"""业务错误：携带可区分的拒绝原因。"""
from __future__ import annotations

from app.models import RejectReason


class ControlPlaneError(Exception):
    def __init__(self, reason: RejectReason, message: str = "") -> None:
        self.reason = reason
        super().__init__(message or reason.value)
