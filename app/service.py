"""业务逻辑：申请/续期/释放/到期回收、配额、权限、审计。

所有会改动状态的操作都在 Store.atomic 内完成，保证：
- 并发申请不会超过配额上限；
- 并发回收同一份额度只释放一次；
- 幂等键重复提交只产生一个环境。
"""
from __future__ import annotations

import time

from app.auth import Identity
from app.errors import ControlPlaneError
from app.models import (
    ALLOWED_TRANSITIONS,
    AuditRecord,
    Environment,
    EnvStatus,
    RejectReason,
)
from app.store import Store

DEFAULT_TTL_SECONDS = 3600.0
MAX_RENEWALS = 3


class EnvService:
    def __init__(
        self,
        store: Store,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_renewals: int = MAX_RENEWALS,
    ) -> None:
        self.store = store
        self.ttl_seconds = ttl_seconds
        self.max_renewals = max_renewals

    # ---- 权限 ----

    def _authorize(self, actor: Identity, env: Environment) -> None:
        """本人、同团队成员或管理员可操作；否则拒绝（即使目标存在）。"""
        if actor.is_admin:
            return
        if actor.user_id == env.owner_id:
            return
        if actor.team_id and actor.team_id == env.team_id:
            return
        raise ControlPlaneError(
            RejectReason.FORBIDDEN, "no permission on this environment"
        )

    # ---- 状态机 ----

    def _transition(
        self,
        env: Environment,
        target: EnvStatus,
        actor_id: str,
        action: str,
        detail: str = "",
    ) -> None:
        if env.status is EnvStatus.RECLAIMED:
            raise ControlPlaneError(
                RejectReason.ALREADY_RECLAIMED,
                f"environment {env.id} already reclaimed",
            )
        if target not in ALLOWED_TRANSITIONS[env.status]:
            raise ControlPlaneError(
                RejectReason.ILLEGAL_TRANSITION,
                f"cannot move {env.status.value} -> {target.value}",
            )
        old = env.status
        env.status = target
        self.store.append_audit(
            AuditRecord(
                env_id=env.id,
                from_status=old,
                to_status=target,
                actor_id=actor_id,
                action=action,
                detail=detail,
            )
        )

    # ---- 申请 ----

    def request_env(self, actor: Identity, idempotency_key: str) -> Environment:
        """申请环境。同一 (owner, idempotency_key) 重复提交返回既有环境。"""
        def _do() -> Environment:
            existing = self.store.find_by_idempotency(actor.user_id, idempotency_key)
            if existing is not None:
                return existing

            uq = self.store.user_quota(actor.user_id)
            tq = self.store.team_quota(actor.team_id)
            if uq.used >= uq.max_envs or tq.used >= tq.max_envs:
                raise ControlPlaneError(
                    RejectReason.QUOTA_EXCEEDED,
                    "user or team quota exceeded",
                )

            env = Environment(
                id=Environment.new_id(),
                owner_id=actor.user_id,
                team_id=actor.team_id,
                idempotency_key=idempotency_key,
                status=EnvStatus.REQUESTED,
                expires_at=time.time() + self.ttl_seconds,
            )
            uq.used += 1
            tq.used += 1
            self.store.put_env(env)
            self.store.bind_idempotency(actor.user_id, idempotency_key, env.id)
            self.store.append_audit(
                AuditRecord(
                    env_id=env.id,
                    from_status=None,
                    to_status=EnvStatus.REQUESTED,
                    actor_id=actor.user_id,
                    action="request",
                )
            )
            return env

        return self.store.atomic(_do)

    # ---- 生命周期推进 ----

    def provision(self, actor: Identity, env_id: str) -> Environment:
        """申请中 -> 准备中 -> 已就绪（本地单进程内同步完成两步）。"""
        def _do() -> Environment:
            env = self._get_or_404(env_id)
            self._authorize(actor, env)
            self._transition(env, EnvStatus.PROVISIONING, actor.user_id, "provision")
            self._transition(env, EnvStatus.READY, actor.user_id, "provision")
            return env

        return self.store.atomic(_do)

    def renew(self, actor: Identity, env_id: str) -> Environment:
        """续期：仅已就绪环境可续，且不超过最大续期次数。"""
        def _do() -> Environment:
            env = self._get_or_404(env_id)
            self._authorize(actor, env)
            if env.status is not EnvStatus.READY:
                if env.status is EnvStatus.RECLAIMED:
                    raise ControlPlaneError(
                        RejectReason.ALREADY_RECLAIMED, "environment reclaimed"
                    )
                raise ControlPlaneError(
                    RejectReason.ILLEGAL_TRANSITION,
                    f"cannot renew in status {env.status.value}",
                )
            if env.renewed_count >= self.max_renewals:
                raise ControlPlaneError(
                    RejectReason.ILLEGAL_TRANSITION, "renewal limit reached"
                )
            env.renewed_count += 1
            env.expires_at = time.time() + self.ttl_seconds
            self.store.append_audit(
                AuditRecord(
                    env_id=env.id,
                    from_status=env.status,
                    to_status=env.status,
                    actor_id=actor.user_id,
                    action="renew",
                    detail=f"renewed_count={env.renewed_count}",
                )
            )
            return env

        return self.store.atomic(_do)

    def release(self, actor: Identity, env_id: str) -> Environment:
        """主动释放：任何未回收状态 -> 已回收，配额立即归还。"""
        def _do() -> Environment:
            env = self._get_or_404(env_id)
            self._authorize(actor, env)
            self._reclaim(env, actor.user_id, action="release")
            return env

        return self.store.atomic(_do)

    def sweep_expired(self, now: float | None = None) -> list[Environment]:
        """到期回收：就绪且已过期的环境进入到期中并回收。

        并发触发时同一份额度只释放一次（由锁与状态检查保证）。
        """
        now = now if now is not None else time.time()

        def _do() -> list[Environment]:
            reclaimed: list[Environment] = []
            for env in self.store.list_envs():
                if env.status is EnvStatus.READY and env.expires_at <= now:
                    self._transition(
                        env, EnvStatus.EXPIRING, "system", "expire", "ttl reached"
                    )
                    self._reclaim(env, "system", action="expire")
                    reclaimed.append(env)
            return reclaimed

        return self.store.atomic(_do)

    def _reclaim(self, env: Environment, actor_id: str, action: str) -> None:
        if env.status is EnvStatus.RECLAIMED:
            raise ControlPlaneError(
                RejectReason.ALREADY_RECLAIMED, "environment already reclaimed"
            )
        self._transition(env, EnvStatus.RECLAIMED, actor_id, action)
        # 归还配额（每份额度只释放一次，因为状态已变为 RECLAIMED）
        uq = self.store.user_quota(env.owner_id)
        tq = self.store.team_quota(env.team_id)
        uq.used = max(0, uq.used - 1)
        tq.used = max(0, tq.used - 1)

    # ---- 查询 ----

    def get_env(self, actor: Identity, env_id: str) -> Environment:
        def _do() -> Environment:
            env = self._get_or_404(env_id)
            self._authorize(actor, env)
            return env

        return self.store.atomic(_do)

    def list_envs(self, actor: Identity) -> list[Environment]:
        def _do() -> list[Environment]:
            envs = self.store.list_envs()
            if actor.is_admin:
                return envs
            return [
                e
                for e in envs
                if e.owner_id == actor.user_id or e.team_id == actor.team_id
            ]

        return self.store.atomic(_do)

    def audit_trail(self, actor: Identity, env_id: str) -> list:
        env = self.get_env(actor, env_id)  # 含权限校验
        return self.store.audit_for(env.id)

    def usage(self, actor: Identity) -> dict:
        def _do() -> dict:
            uq = self.store.user_quota(actor.user_id)
            tq = self.store.team_quota(actor.team_id)
            return {
                "user": {"used": uq.used, "max": uq.max_envs},
                "team": {"used": tq.used, "max": tq.max_envs},
            }

        return self.store.atomic(_do)

    # ---- 内部 ----

    def _get_or_404(self, env_id: str) -> Environment:
        env = self.store.get_env(env_id)
        if env is None:
            raise ControlPlaneError(RejectReason.NOT_FOUND, "environment not found")
        return env
