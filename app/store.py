"""本地内存存储：环境、审计、配额与幂等索引。

单进程内使用 threading.RLock 保证并发安全；
Service 层可在同一把锁内组合多个存储操作，保证配额检查与占用原子化。
"""
from __future__ import annotations

import threading

from .models import ACTIVE_STATUSES, AuditRecord, Environment, Quota

DEFAULT_USER_QUOTA = 3
DEFAULT_TEAM_QUOTA = 8


class Store:
    """单进程本地存储。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._envs: dict[str, Environment] = {}
        self._audits: dict[str, list[AuditRecord]] = {}
        self._user_quota: dict[str, Quota] = {}
        self._team_quota: dict[str, Quota] = {}
        self._idempotency: dict[tuple[str, str], str] = {}

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    # --- 环境 ---
    def get_env(self, env_id: str) -> Environment | None:
        with self._lock:
            return self._envs.get(env_id)

    def put_env(self, env: Environment) -> None:
        with self._lock:
            self._envs[env.env_id] = env

    def list_envs(self) -> list[Environment]:
        with self._lock:
            return list(self._envs.values())

    # --- 幂等 ---
    def get_by_idempotency(self, owner_id: str, key: str) -> Environment | None:
        with self._lock:
            env_id = self._idempotency.get((owner_id, key))
            return self._envs.get(env_id) if env_id else None

    def bind_idempotency(self, owner_id: str, key: str, env_id: str) -> None:
        with self._lock:
            self._idempotency[(owner_id, key)] = env_id

    # --- 配额 ---
    def get_user_quota(self, user_id: str) -> Quota:
        with self._lock:
            return self._user_quota.setdefault(user_id, Quota(max_envs=DEFAULT_USER_QUOTA))

    def get_team_quota(self, team_id: str) -> Quota:
        with self._lock:
            return self._team_quota.setdefault(team_id, Quota(max_envs=DEFAULT_TEAM_QUOTA))

    def count_active_by_user(self, user_id: str) -> int:
        with self._lock:
            return sum(
                1 for e in self._envs.values()
                if e.owner_id == user_id and e.status in ACTIVE_STATUSES
            )

    def count_active_by_team(self, team_id: str) -> int:
        with self._lock:
            return sum(
                1 for e in self._envs.values()
                if e.team_id == team_id and e.status in ACTIVE_STATUSES
            )

    # --- 审计 ---
    def append_audit(self, record: AuditRecord) -> None:
        with self._lock:
            self._audits.setdefault(record.env_id, []).append(record)

    def list_audits(self, env_id: str) -> list[AuditRecord]:
        with self._lock:
            return list(self._audits.get(env_id, []))
