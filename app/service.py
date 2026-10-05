"""控制面业务逻辑：生命周期、配额、权限、幂等、审计。

并发安全：所有"检查 + 变更"的复合操作都在 store.lock 内完成，
保证并发申请不超配额、并发回收同一份额度只释放一次。
"""
from __future__ import annotations

from .errors import (
    AlreadyReclaimedError,
    ForbiddenError,
    InvalidTransitionError,
    NotFoundError,
    QuotaExceededError,
)
from .models import (
    ACTIVE_STATUSES,
    ALLOWED_TRANSITIONS,
    AuditRecord,
    Environment,
    EnvStatus,
    Principal,
    Role,
    new_id,
    now,
)
from .store import Store

DEFAULT_TTL_SECONDS = 3600.0
MAX_TTL_SECONDS = 4 * 3600.0


class EnvService:
    def __init__(self, store: Store) -> None:
        self._store = store

    # ---- 内部工具 ----

    def _check_permission(self, principal: Principal, env: Environment) -> None:
        """本人、同团队成员或管理员可操作；越权一律 403 且不泄露额外信息。"""
        if principal.role is Role.ADMIN:
            return
        if env.owner_id == principal.user_id:
            return
        if env.team_id == principal.team_id:
            return
        raise ForbiddenError(
            f"principal {principal.user_id} has no access to env {env.env_id}"
        )

    def _get_env_or_404(self, env_id: str) -> Environment:
        env = self._store.get_env(env_id)
        if env is None:
            raise NotFoundError(f"env {env_id} not found")
        return env

    def _transition(self, env: Environment, to: EnvStatus, actor: str,
                    action: str, detail: str = "") -> None:
        """状态迁移 + 审计。调用方必须持有 store.lock。"""
        if env.status is EnvStatus.RECLAIMED:
            raise AlreadyReclaimedError(f"env {env.env_id} already reclaimed")
        if to not in ALLOWED_TRANSITIONS[env.status]:
            raise InvalidTransitionError(
                f"cannot move env {env.env_id} from {env.status.value} to {to.value}"
            )
        frm = env.status
        env.status = to
        env.version += 1
        env.updated_at = now()
        self._store.put_env(env)
        self._store.append_audit(AuditRecord(
            audit_id=new_id(), env_id=env.env_id, actor_id=actor, action=action,
            from_status=frm, to_status=to, detail=detail, at=now(),
        ))

    # ---- 申请（幂等 + 配额） ----

    def apply(self, principal: Principal, idempotency_key: str,
              ttl_seconds: float = DEFAULT_TTL_SECONDS) -> Environment:
        if not idempotency_key:
            raise InvalidTransitionError("idempotency_key is required")
        ttl = min(max(ttl_seconds, 60.0), MAX_TTL_SECONDS)
        with self._store.lock:
            # 幂等：同一 (用户, 幂等键) 只产生一个环境，重试直接返回既有环境
            existing = self._store.get_by_idempotency(principal.user_id, idempotency_key)
            if existing is not None:
                return existing
            # 配额：用户与团队两个维度，检查与占用在同一把锁内原子完成
            user_q = self._store.get_user_quota(principal.user_id)
            if self._store.count_active_by_user(principal.user_id) >= user_q.max_envs:
                raise QuotaExceededError(
                    f"user {principal.user_id} quota exceeded ({user_q.max_envs})"
                )
            team_q = self._store.get_team_quota(principal.team_id)
            if self._store.count_active_by_team(principal.team_id) >= team_q.max_envs:
                raise QuotaExceededError(
                    f"team {principal.team_id} quota exceeded ({team_q.max_envs})"
                )
            ts = now()
            env = Environment(
                env_id=new_id(), owner_id=principal.user_id, team_id=principal.team_id,
                status=EnvStatus.REQUESTED, idempotency_key=idempotency_key,
                created_at=ts, updated_at=ts, expires_at=ts + ttl,
            )
            self._store.put_env(env)
            self._store.bind_idempotency(principal.user_id, idempotency_key, env.env_id)
            self._store.append_audit(AuditRecord(
                audit_id=new_id(), env_id=env.env_id, actor_id=principal.user_id,
                action="apply", from_status=None, to_status=EnvStatus.REQUESTED,
                detail=f"ttl={ttl}s", at=ts,
            ))
            # 本地单进程：准备过程同步完成，沿允许路径推进到就绪
            self._transition(env, EnvStatus.PROVISIONING, "system", "provision")
            self._transition(env, EnvStatus.READY, "system", "provision")
            return env

    # ---- 续期 ----

    def renew(self, principal: Principal, env_id: str,
              extend_seconds: float = DEFAULT_TTL_SECONDS) -> Environment:
        with self._store.lock:
            env = self._get_env_or_404(env_id)
            self._check_permission(principal, env)
            if env.status is EnvStatus.RECLAIMED:
                raise AlreadyReclaimedError(f"env {env_id} already reclaimed")
            if env.status is not EnvStatus.READY:
                raise InvalidTransitionError(
                    f"only ready env can be renewed, current={env.status.value}"
                )
            ext = min(max(extend_seconds, 60.0), MAX_TTL_SECONDS)
            env.expires_at = min(now() + ext, env.created_at + MAX_TTL_SECONDS)
            env.version += 1
            env.updated_at = now()
            self._store.put_env(env)
            self._store.append_audit(AuditRecord(
                audit_id=new_id(), env_id=env.env_id, actor_id=principal.user_id,
                action="renew", from_status=env.status, to_status=env.status,
                detail=f"extend={ext}s expires_at={env.expires_at}", at=now(),
            ))
            return env

    # ---- 释放 ----

    def release(self, principal: Principal, env_id: str) -> Environment:
        with self._store.lock:
            env = self._get_env_or_404(env_id)
            self._check_permission(principal, env)
            self._transition(env, EnvStatus.RECLAIMED, principal.user_id, "release")
            return env

    # ---- 到期回收 ----

    def expire_due(self, at: float | None = None) -> list[Environment]:
        """把到期环境沿 READY->EXPIRING->RECLAIMED 回收。

        并发触发时由锁保证同一环境只回收一次，额度只释放一次。
        """
        ts = now() if at is None else at
        reclaimed: list[Environment] = []
        with self._store.lock:
            due = [
                e for e in self._store.list_envs()
                if e.status in ACTIVE_STATUSES and e.expires_at <= ts
            ]
            for env in due:
                if env.status is EnvStatus.READY:
                    self._transition(env, EnvStatus.EXPIRING, "system", "expire",
                                     detail="ttl reached")
                self._transition(env, EnvStatus.RECLAIMED, "system", "reclaim",
                                 detail="quota freed")
                reclaimed.append(env)
        return reclaimed

    # ---- 查询 ----

    def get(self, principal: Principal, env_id: str) -> Environment:
        with self._store.lock:
            env = self._get_env_or_404(env_id)
            self._check_permission(principal, env)
            return env

    def list_visible(self, principal: Principal) -> list[Environment]:
        with self._store.lock:
            envs = self._store.list_envs()
            if principal.role is Role.ADMIN:
                return envs
            return [
                e for e in envs
                if e.owner_id == principal.user_id or e.team_id == principal.team_id
            ]

    def audits(self, principal: Principal, env_id: str) -> list[AuditRecord]:
        with self._store.lock:
            env = self._get_env_or_404(env_id)
            self._check_permission(principal, env)
            return self._store.list_audits(env_id)

    def usage(self, principal: Principal) -> dict:
        with self._store.lock:
            user_q = self._store.get_user_quota(principal.user_id)
            team_q = self._store.get_team_quota(principal.team_id)
            return {
                "user": {
                    "user_id": principal.user_id,
                    "quota": user_q.max_envs,
                    "active": self._store.count_active_by_user(principal.user_id),
                },
                "team": {
                    "team_id": principal.team_id,
                    "quota": team_q.max_envs,
                    "active": self._store.count_active_by_team(principal.team_id),
                },
            }
