"""控制面业务逻辑（骨架：空实现）。"""
from __future__ import annotations

from .models import AuditRecord, Environment, Principal
from .store import Store

DEFAULT_TTL_SECONDS = 3600.0
MAX_TTL_SECONDS = 4 * 3600.0


class EnvService:
    def __init__(self, store: Store) -> None:
        self._store = store

    def apply(self, principal: Principal, env_id: str, idempotency_key: str,
              ttl_seconds: float = DEFAULT_TTL_SECONDS) -> Environment:
        """申请环境（幂等）。"""
        raise NotImplementedError

    def renew(self, principal: Principal, env_id: str,
              extend_seconds: float = DEFAULT_TTL_SECONDS) -> Environment:
        """续期。"""
        raise NotImplementedError

    def release(self, principal: Principal, env_id: str) -> Environment:
        """主动释放。"""
        raise NotImplementedError

    def expire_due(self, at: float | None = None) -> list[Environment]:
        """到期回收（可并发安全，同一环境只回收一次）。"""
        raise NotImplementedError

    def get(self, principal: Principal, env_id: str) -> Environment:
        """按权限查看。"""
        raise NotImplementedError

    def list_visible(self, principal: Principal) -> list[Environment]:
        raise NotImplementedError

    def audits(self, principal: Principal, env_id: str) -> list[AuditRecord]:
        raise NotImplementedError

    def usage(self, principal: Principal) -> dict:
        """配额与占用统计。"""
        raise NotImplementedError
