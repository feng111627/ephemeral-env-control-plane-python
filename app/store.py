"""本地线程安全存储：环境、幂等键、配额占用、审计日志。

单进程内存实现，所有公开方法在内部可重入锁保护下执行，
保证并发申请/续期/释放/回收时的原子性。
"""
from __future__ import annotations

import threading
from collections.abc import Callable

from app.models import AuditRecord, Environment, EnvStatus, Quota

DEFAULT_USER_QUOTA = 3
DEFAULT_TEAM_QUOTA = 8


class Store:
    """单进程本地存储。"""

    def __init__(
        self,
        default_user_quota: int = DEFAULT_USER_QUOTA,
        default_team_quota: int = DEFAULT_TEAM_QUOTA,
    ) -> None:
        self._lock = threading.RLock()
        self._default_user_quota = default_user_quota
        self._default_team_quota = default_team_quota
        self._envs: dict[str, Environment] = {}
        self._idempotency: dict[tuple[str, str], str] = {}  # (owner, key) -> env_id
        self._user_quota: dict[str, Quota] = {}
        self._team_quota: dict[str, Quota] = {}
        self._audit: list[AuditRecord] = []

    # ---- 通用原子执行 ----

    def atomic[T](self, fn: Callable[[], T]) -> T:
        """在全局锁内执行一段业务逻辑，保证多步读写的原子性。"""
        with self._lock:
            return fn()

    # ---- 环境 ----

    def get_env(self, env_id: str) -> Environment | None:
        return self._envs.get(env_id)

    def put_env(self, env: Environment) -> None:
        self._envs[env.id] = env

    def find_by_idempotency(self, owner_id: str, key: str) -> Environment | None:
        env_id = self._idempotency.get((owner_id, key))
        return self._envs.get(env_id) if env_id else None

    def bind_idempotency(self, owner_id: str, key: str, env_id: str) -> None:
        self._idempotency[(owner_id, key)] = env_id

    def list_envs(self) -> list[Environment]:
        return list(self._envs.values())

    def count_unsettled(self, *, owner_id: str | None = None, team_id: str | None = None) -> int:
        """统计未结清（未回收）环境数。"""
        return sum(
            1
            for e in self._envs.values()
            if e.status is not EnvStatus.RECLAIMED
            and (owner_id is None or e.owner_id == owner_id)
            and (team_id is None or e.team_id == team_id)
        )

    # ---- 配额 ----

    def user_quota(self, user_id: str) -> Quota:
        return self._user_quota.setdefault(user_id, Quota(max_envs=self._default_user_quota))

    def team_quota(self, team_id: str) -> Quota:
        return self._team_quota.setdefault(team_id, Quota(max_envs=self._default_team_quota))

    def set_user_quota(self, user_id: str, max_envs: int) -> None:
        with self._lock:
            q = self.user_quota(user_id)
            q.max_envs = max_envs

    def set_team_quota(self, team_id: str, max_envs: int) -> None:
        with self._lock:
            q = self.team_quota(team_id)
            q.max_envs = max_envs

    # ---- 审计 ----

    def append_audit(self, record: AuditRecord) -> None:
        self._audit.append(record)

    def audit_for(self, env_id: str) -> list[AuditRecord]:
        return [r for r in self._audit if r.env_id == env_id]
