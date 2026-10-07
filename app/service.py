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
    QueueAuditRecord,
    QueueEntry,
    QueueStatus,
    RejectReason,
)
from app.store import Store

DEFAULT_TTL_SECONDS = 3600.0
MAX_RENEWALS = 3
DEFAULT_QUEUE_TTL_SECONDS = 1800.0


class EnvService:
    def __init__(
        self,
        store: Store,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_renewals: int = MAX_RENEWALS,
        queue_ttl_seconds: float = DEFAULT_QUEUE_TTL_SECONDS,
    ) -> None:
        self.store = store
        self.ttl_seconds = ttl_seconds
        self.max_renewals = max_renewals
        self.queue_ttl_seconds = queue_ttl_seconds

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
            if not self._quota_available(actor.user_id, actor.team_id):
                raise ControlPlaneError(
                    RejectReason.QUOTA_EXCEEDED,
                    "user or team quota exceeded",
                )
            return self._create_env_locked(
                actor.user_id, actor.team_id, idempotency_key, actor.user_id, "request"
            )

        return self.store.atomic(_do)

    # ---- 内部：配额与建环境（须在锁内调用） ----

    def _quota_available(self, user_id: str, team_id: str) -> bool:
        uq = self.store.user_quota(user_id)
        tq = self.store.team_quota(team_id)
        return uq.used < uq.max_envs and tq.used < tq.max_envs

    def _create_env_locked(
        self, owner_id: str, team_id: str, key: str, actor_id: str, action: str
    ) -> Environment:
        """在锁内创建环境并占用配额（调用前须确认幂等键未绑定、配额充足）。"""
        env = Environment(
            id=Environment.new_id(),
            owner_id=owner_id,
            team_id=team_id,
            idempotency_key=key,
            status=EnvStatus.REQUESTED,
            expires_at=time.time() + self.ttl_seconds,
        )
        self.store.user_quota(owner_id).used += 1
        self.store.team_quota(team_id).used += 1
        self.store.put_env(env)
        self.store.bind_idempotency(owner_id, key, env.id)
        self.store.append_audit(
            AuditRecord(
                env_id=env.id,
                from_status=None,
                to_status=EnvStatus.REQUESTED,
                actor_id=actor_id,
                action=action,
            )
        )
        return env

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
        回收归还额度后自动放行排队条目；随后兜底处理超时排队记录。
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
            self._expire_queue_entries_locked(now)
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
        # 额度释放后按入队先后自动放行（同一锁内，不超发、不漏放）
        self._promote_queue_locked(env.team_id)

    # ---- 排队 ----

    def enqueue(self, actor: Identity, idempotency_key: str) -> QueueEntry:
        """带幂等键排队等额度。

        - 同键已有环境：直接返回关联该环境的已放行记录（幂等，不多建）；
        - 同键已有排队中的记录：返回原记录（同人同键只占一个位置）；
        - 团队队列无人在等且额度充足：立即放行（与直接申请等价）；
        - 否则排到队尾。取消/超时后再排会生成新记录、排到队尾。
        """
        def _do() -> QueueEntry:
            existing_env = self.store.find_by_idempotency(actor.user_id, idempotency_key)
            if existing_env is not None:
                return self._record_entry(
                    actor, idempotency_key, QueueStatus.PROMOTED,
                    env_id=existing_env.id, detail="env already exists",
                )

            active = self.store.find_active_queue_entry(actor.user_id, idempotency_key)
            if active is not None:
                return active

            if not self.store.waiting_entries(actor.team_id) and self._quota_available(
                actor.user_id, actor.team_id
            ):
                env = self._create_env_locked(
                    actor.user_id, actor.team_id, idempotency_key,
                    actor.user_id, "enqueue",
                )
                return self._record_entry(
                    actor, idempotency_key, QueueStatus.PROMOTED,
                    env_id=env.id, detail="quota available, promoted immediately",
                )

            return self._record_entry(actor, idempotency_key, QueueStatus.WAITING)

        return self.store.atomic(_do)

    def _record_entry(
        self,
        actor: Identity,
        key: str,
        status: QueueStatus,
        env_id: str | None = None,
        detail: str = "",
    ) -> QueueEntry:
        entry = QueueEntry(
            id=QueueEntry.new_id(),
            owner_id=actor.user_id,
            team_id=actor.team_id,
            idempotency_key=key,
            status=status,
            expires_at=time.time() + self.queue_ttl_seconds,
            env_id=env_id,
        )
        self.store.put_queue_entry(entry)
        self.store.append_queue_audit(
            QueueAuditRecord(
                entry_id=entry.id,
                from_status=None,
                to_status=status,
                actor_id=actor.user_id,
                action="enqueue",
                detail=detail,
            )
        )
        return entry

    def get_queue_entry(self, actor: Identity, entry_id: str) -> QueueEntry:
        """查看排队状态：仅本人或管理员。"""
        def _do() -> QueueEntry:
            entry = self._get_entry_or_404(entry_id)
            self._authorize_queue(actor, entry)
            return entry

        return self.store.atomic(_do)

    def cancel_queue_entry(self, actor: Identity, entry_id: str) -> QueueEntry:
        """主动放弃排队：仅本人或管理员；仅排队中的记录可取消。"""
        def _do() -> QueueEntry:
            entry = self._get_entry_or_404(entry_id)
            self._authorize_queue(actor, entry)
            if entry.status is QueueStatus.CANCELLED:
                return entry  # 重复取消幂等
            if entry.status is not QueueStatus.WAITING:
                raise ControlPlaneError(
                    RejectReason.ILLEGAL_TRANSITION,
                    f"cannot cancel entry in status {entry.status.value}",
                )
            self._queue_transition(entry, QueueStatus.CANCELLED, actor.user_id, "cancel")
            return entry

        return self.store.atomic(_do)

    def list_queue(self, actor: Identity) -> list[QueueEntry]:
        """列出排队记录：本人全部记录；管理员全量。"""
        def _do() -> list[QueueEntry]:
            if actor.is_admin:
                return self.store.list_queue_entries()
            return self.store.list_queue_entries(owner_id=actor.user_id)

        return self.store.atomic(_do)

    def queue_audit_trail(self, actor: Identity, entry_id: str) -> list:
        entry = self.get_queue_entry(actor, entry_id)  # 含权限校验
        return self.store.queue_audit_for(entry.id)

    def sweep_queue(self, now: float | None = None) -> list[QueueEntry]:
        """排队超时兜底：排队中且超过有效期的记录自动失效。"""
        now = now if now is not None else time.time()

        def _do() -> list[QueueEntry]:
            return self._expire_queue_entries_locked(now)

        return self.store.atomic(_do)

    def _expire_queue_entries_locked(self, now: float) -> list[QueueEntry]:
        expired: list[QueueEntry] = []
        for entry in self.store.list_queue_entries():
            if entry.status is QueueStatus.WAITING and entry.expires_at <= now:
                self._queue_transition(entry, QueueStatus.EXPIRED, "system", "expire",
                                       "queue ttl reached")
                expired.append(entry)
        return expired

    def _promote_queue_locked(self, team_id: str) -> list[QueueEntry]:
        """额度释放后按入队先后自动放行（严格 FIFO，须在锁内调用）。

        遇到第一个无法满足配额的条目即停止，保证先排的先拿到、不超发。
        若条目的幂等键在此期间已被手动重试绑定环境，则直接关联该环境，
        不会重复创建。
        """
        promoted: list[QueueEntry] = []
        for entry in self.store.waiting_entries(team_id):
            if not self._quota_available(entry.owner_id, entry.team_id):
                break
            env = self.store.find_by_idempotency(entry.owner_id, entry.idempotency_key)
            if env is None:
                env = self._create_env_locked(
                    entry.owner_id, entry.team_id, entry.idempotency_key,
                    "system", "promote",
                )
                detail = f"env_id={env.id}"
            else:
                detail = f"env_id={env.id} (already created)"
            entry.env_id = env.id
            self._queue_transition(entry, QueueStatus.PROMOTED, "system", "promote",
                                   detail)
            promoted.append(entry)
        return promoted

    def _queue_transition(
        self,
        entry: QueueEntry,
        target: QueueStatus,
        actor_id: str,
        action: str,
        detail: str = "",
    ) -> None:
        old = entry.status
        entry.status = target
        self.store.append_queue_audit(
            QueueAuditRecord(
                entry_id=entry.id,
                from_status=old,
                to_status=target,
                actor_id=actor_id,
                action=action,
                detail=detail,
            )
        )

    def _authorize_queue(self, actor: Identity, entry: QueueEntry) -> None:
        """排队记录仅本人或管理员可见可操作（同团队成员也不行）。"""
        if actor.is_admin or actor.user_id == entry.owner_id:
            return
        raise ControlPlaneError(
            RejectReason.FORBIDDEN, "no permission on this queue entry"
        )

    def _get_entry_or_404(self, entry_id: str) -> QueueEntry:
        entry = self.store.get_queue_entry(entry_id)
        if entry is None:
            raise ControlPlaneError(RejectReason.NOT_FOUND, "queue entry not found")
        return entry

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
