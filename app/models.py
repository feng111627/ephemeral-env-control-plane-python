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
    ALREADY_REQUESTED = "already_requested"
    WAITLIST_NOT_WAITING = "waitlist_not_waiting"


class WaitStatus(str, enum.Enum):
    """排队记录的生命周期状态。"""

    WAITING = "waiting"      # 排队中
    ADMITTED = "admitted"    # 已放行（已自动创建环境）
    CANCELLED = "cancelled"  # 已取消（主动放弃）
    EXPIRED = "expired"      # 已超时失效


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


@dataclass
class WaitEntry:
    """一次排队等候记录。

    幂等域为 (owner_id, idempotency_key)：同一人同一键在排队中重复排队
    返回同一条记录；取消/失效/放行后再排则新建记录（重新排到队尾）。
    """

    id: str
    owner_id: str
    team_id: str
    idempotency_key: str
    seq: int                          # 全局单调递增的入队序号，决定先后顺序
    status: WaitStatus = WaitStatus.WAITING
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    admitted_at: float | None = None
    finished_at: float | None = None
    env_id: str | None = None         # 放行后创建的环境 id

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex


@dataclass
class WaitAuditRecord:
    """排队生命周期审计：排队/放行/取消/超时 每一步都留痕。"""

    wait_id: str
    owner_id: str
    team_id: str
    idempotency_key: str
    from_status: WaitStatus | None
    to_status: WaitStatus
    actor_id: str
    action: str
    at: float = field(default_factory=time.time)
    detail: str = ""
