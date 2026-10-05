"""领域模型与类型定义（骨架：仅类型，无实现）。"""
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


# 允许的状态迁移路径
ALLOWED_TRANSITIONS: dict[EnvStatus, set[EnvStatus]] = {
    EnvStatus.REQUESTED: {EnvStatus.PROVISIONING, EnvStatus.RECLAIMED},
    EnvStatus.PROVISIONING: {EnvStatus.READY, EnvStatus.RECLAIMED},
    EnvStatus.READY: {EnvStatus.EXPIRING, EnvStatus.RECLAIMED},
    EnvStatus.EXPIRING: {EnvStatus.RECLAIMED},
    EnvStatus.RECLAIMED: set(),
}


class Role(str, enum.Enum):
    ADMIN = "admin"
    MEMBER = "member"


@dataclass
class Principal:
    """调用方身份。"""

    user_id: str
    team_id: str
    role: Role = Role.MEMBER


@dataclass
class Environment:
    env_id: str
    owner_id: str
    team_id: str
    status: EnvStatus
    idempotency_key: str
    created_at: float
    updated_at: float
    expires_at: float
    version: int = 0


@dataclass
class AuditRecord:
    audit_id: str
    env_id: str
    actor_id: str
    action: str
    from_status: EnvStatus | None
    to_status: EnvStatus | None
    detail: str
    at: float


@dataclass
class Quota:
    """用户/团队维度配额。"""

    max_envs: int


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> float:
    return time.time()
