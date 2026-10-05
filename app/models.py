"""领域模型：环境状态机、环境记录、配额、审计记录。"""
from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field


class EnvStatus(str, enum.Enum):
    """环境生命周期状态。"""

    REQUESTED = "requested"      # 申请中
    PROVISIONING = "provisioning"  # 准备中
    READY = "ready"              # 已就绪
    EXPIRING = "expiring"        # 到期中
    RECLAIMED = "reclaimed"      # 已回收


#: 允许的状态迁移路径
ALLOWED_TRANSITIONS: dict[EnvStatus, set[EnvStatus]] = {
    EnvStatus.REQUESTED: {EnvStatus.PROVISIONING, EnvStatus.RECLAIMED},
    EnvStatus.PROVISIONING: {EnvStatus.READY, EnvStatus.RECLAIMED},
    EnvStatus.READY: {EnvStatus.EXPIRING, EnvStatus.RECLAIMED},
    EnvStatus.EXPIRING: {EnvStatus.RECLAIMED},
    EnvStatus.RECLAIMED: set(),
}


class RejectReason(str, enum.Enum):
    """可区分的拒绝原因。"""

    ILLEGAL_TRANSITION = "illegal_transition"
    ALREADY_RECLAIMED = "already_reclaimed"
    QUOTA_EXCEEDED = "quota_exceeded"
    UNSETTLED_ENVS = "unsettled_envs"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"


@dataclass
class Environment:
    id: str
    owner_id: str
    team_id: str
    idempotency_key: str
    status: EnvStatus = EnvStatus.REQUESTED
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    renewed_count: int = 0

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex


@dataclass
class Quota:
    """单维度（用户或团队）配额。"""

    max_envs: int
    used: int = 0


@dataclass
class AuditRecord:
    env_id: str
    from_status: EnvStatus | None
    to_status: EnvStatus
    actor_id: str
    action: str
    at: float = field(default_factory=time.time)
    detail: str = ""
