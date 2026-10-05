"""本地内存存储（骨架：接口与空实现）。"""
from __future__ import annotations

import threading

from .models import AuditRecord, Environment, Quota


class Store:
    """单进程本地存储，持有环境、审计、配额与幂等索引。"""

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
        raise NotImplementedError

    def put_env(self, env: Environment) -> None:
        raise NotImplementedError

    def list_envs(self) -> list[Environment]:
        raise NotImplementedError

    # --- 幂等 ---
    def get_by_idempotency(self, owner_id: str, key: str) -> Environment | None:
        raise NotImplementedError

    def bind_idempotency(self, owner_id: str, key: str, env_id: str) -> None:
        raise NotImplementedError

    # --- 配额 ---
    def get_user_quota(self, user_id: str) -> Quota:
        raise NotImplementedError

    def get_team_quota(self, team_id: str) -> Quota:
        raise NotImplementedError

    def count_active_by_user(self, user_id: str) -> int:
        raise NotImplementedError

    def count_active_by_team(self, team_id: str) -> int:
        raise NotImplementedError

    # --- 审计 ---
    def append_audit(self, record: AuditRecord) -> None:
        raise NotImplementedError

    def list_audits(self, env_id: str) -> list[AuditRecord]:
        raise NotImplementedError
