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
    WaitAuditRecord,
    WaitEntry,
    WaitStatus,
)
from app.store import Store

DEFAULT_TTL_SECONDS = 3600.0
MAX_RENEWALS = 3
DEFAULT_WAIT_TTL_SECONDS = 1800.0


class EnvService:
    def __init__(
        self,
        store: Store,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_renewals: int = MAX_RENEWALS,
        wait_ttl_seconds: float = DEFAULT_WAIT_TTL_SECONDS,
    ) -> None:
        self.store = store
        self.ttl_seconds = ttl_seconds
        self.max_renewals = max_renewals
        self.wait_ttl_seconds = wait_ttl_seconds

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
                env = existing
            else:
                env = self._create_env_locked(
                    actor.user_id, actor.team_id, idempotency_key, time.time()
                )
            # 该键若正在排队（用户在自动放行前手动重试成功），把排队记录收敛为
            # "已放行"并指向这个已存在的环境：绝不重复建环境、不重复占额度。
            waiting = self.store.find_waiting_by_key(actor.user_id, idempotency_key)
            if (
                waiting is not None
                and waiting.status is WaitStatus.WAITING
                and env.status is not EnvStatus.RECLAIMED
            ):
                self._converge_wait_locked(waiting, env, time.time())
            return env

        return self.store.atomic(_do)

    def _create_env_locked(
        self,
        owner_id: str,
        team_id: str,
        idempotency_key: str,
        now: float,
        detail: str = "",
    ) -> Environment:
        """扣配额并创建环境。调用方须持锁；与手动申请走完全相同的语义。"""
        uq = self.store.user_quota(owner_id)
        tq = self.store.team_quota(team_id)
        if uq.used >= uq.max_envs or tq.used >= tq.max_envs:
            raise ControlPlaneError(
                RejectReason.QUOTA_EXCEEDED,
                "user or team quota exceeded",
            )
        env = Environment(
            id=Environment.new_id(),
            owner_id=owner_id,
            team_id=team_id,
            idempotency_key=idempotency_key,
            status=EnvStatus.REQUESTED,
            expires_at=now + self.ttl_seconds,
        )
        uq.used += 1
        tq.used += 1
        self.store.put_env(env)
        self.store.bind_idempotency(owner_id, idempotency_key, env.id)
        self.store.append_audit(
            AuditRecord(
                env_id=env.id,
                from_status=None,
                to_status=EnvStatus.REQUESTED,
                actor_id=owner_id,
                action="request",
                detail=detail,
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
        # 额度释放后，在同一原子事务内尝试放行该团队的排队者。
        # 与外部手动重试申请共用同一把全局锁，因此不会多建环境。
        self._promote_team_locked(env.team_id)

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

    # ===================================================================
    # 排队等候（waitlist）
    # ===================================================================

    # ---- 排队权限：排队记录比环境更私密，仅本人与管理员可操作 ----

    def _authorize_wait(self, actor: Identity, entry: WaitEntry) -> None:
        if actor.is_admin or actor.user_id == entry.owner_id:
            return
        raise ControlPlaneError(
            RejectReason.FORBIDDEN, "no permission on this waitlist entry"
        )

    def _get_wait_or_404(self, wait_id: str) -> WaitEntry:
        entry = self.store.get_wait_entry(wait_id)
        if entry is None:
            raise ControlPlaneError(
                RejectReason.NOT_FOUND, "waitlist entry not found"
            )
        return entry

    # ---- 排队状态流转与审计（调用方须持锁） ----

    def _finish_wait_locked(
        self,
        entry: WaitEntry,
        status: WaitStatus,
        actor_id: str,
        action: str,
        now: float,
        detail: str = "",
        env_id: str | None = None,
    ) -> None:
        old = entry.status
        entry.status = status
        entry.finished_at = now
        if status is WaitStatus.ADMITTED:
            entry.admitted_at = now
        if env_id is not None:
            entry.env_id = env_id
        # 只有"排队中"的记录占用 (owner,key) 幂等位
        self.store.unbind_wait_key(entry.owner_id, entry.idempotency_key)
        self.store.append_wait_audit(
            WaitAuditRecord(
                wait_id=entry.id,
                owner_id=entry.owner_id,
                team_id=entry.team_id,
                idempotency_key=entry.idempotency_key,
                from_status=old,
                to_status=status,
                actor_id=actor_id,
                action=action,
                at=now,
                detail=detail,
            )
        )

    def _converge_wait_locked(
        self, entry: WaitEntry, env: Environment, now: float
    ) -> None:
        """排队记录被一个已存在的环境收敛为"已放行"（不重复建环境/占额度）。"""
        self._finish_wait_locked(
            entry,
            WaitStatus.ADMITTED,
            env.owner_id,
            "admit_manual",
            now,
            detail=f"converged to existing env {env.id}",
            env_id=env.id,
        )

    def _expire_wait_locked(self, entry: WaitEntry, now: float) -> bool:
        if entry.status is WaitStatus.WAITING and entry.expires_at <= now:
            self._finish_wait_locked(
                entry, WaitStatus.EXPIRED, "system", "expire_wait", now,
                detail="wait ttl reached",
            )
            return True
        return False

    # ---- 核心：团队内按序放行（调用方须持全局锁） ----

    def _promote_team_locked(self, team_id: str, now: float | None = None) -> None:
        """额度可能被释放后调用：先清理队头超时，再严格按入队顺序放行。

        - 先排先得（seq 升序），不跳过队头任何仍有效的排队者；
        - 每份释放出的额度在同一原子事务内消费，绝不突破用户/团队上限；
        - 与用户手动重试申请共用全局锁，幂等位检查保证同一键不会多建环境。
        """
        now = now if now is not None else time.time()
        while True:
            queue = self.store.waiting_entries_for_team(team_id)
            if not queue:
                return
            head = queue[0]
            # 队头超时：失效后继续看后面的人（超时失效不占用放行额度）
            if head.expires_at <= now:
                self._expire_wait_locked(head, now)
                continue
            uq = self.store.user_quota(head.owner_id)
            tq = self.store.team_quota(head.team_id)
            if uq.used >= uq.max_envs or tq.used >= tq.max_envs:
                # 严格 FIFO：队头因用户自身额度满等原因暂时进不来，后面不越过
                return
            # 幂等兜底：同一键已经有环境（与手动重试竞争时），直接收敛
            existing = self.store.find_by_idempotency(
                head.owner_id, head.idempotency_key
            )
            if existing is not None:
                self._converge_wait_locked(head, existing, now)
                continue
            env = self._create_env_locked(
                head.owner_id,
                head.team_id,
                head.idempotency_key,
                now,
                detail="auto-admitted from waitlist",
            )
            self._finish_wait_locked(
                head,
                WaitStatus.ADMITTED,
                "system",
                "admit_auto",
                now,
                detail=f"env {env.id}",
                env_id=env.id,
            )

    # ---- 对外服务接口 ----

    def join_waitlist(
        self, actor: Identity, idempotency_key: str
    ) -> tuple[WaitEntry, Environment | None]:
        """排队等候。返回 (排队记录, 若立即放行则为环境，否则 None)。

        同一 (owner, key) 仍在排队中则返回同一条记录（不重复占位）；
        该键已有环境（含已回收）则拒绝，语义与申请接口的幂等解释一致。
        入队时若额度恰好可用，立即放行。
        """
        def _do() -> tuple[WaitEntry, Environment | None]:
            now = time.time()
            existing_env = self.store.find_by_idempotency(
                actor.user_id, idempotency_key
            )
            if existing_env is not None:
                raise ControlPlaneError(
                    RejectReason.ALREADY_REQUESTED,
                    "idempotency key already has an environment",
                )
            waiting = self.store.find_waiting_by_key(actor.user_id, idempotency_key)
            if waiting is not None:
                if waiting.status is WaitStatus.WAITING:
                    if waiting.expires_at <= now:
                        self._expire_wait_locked(waiting, now)
                    else:
                        return waiting, None
                # 已取消/超时后再排：落入下方新建（重新排到队尾）
            entry = WaitEntry(
                id=WaitEntry.new_id(),
                owner_id=actor.user_id,
                team_id=actor.team_id,
                idempotency_key=idempotency_key,
                seq=self.store.next_wait_seq(),
                status=WaitStatus.WAITING,
                created_at=now,
                expires_at=now + self.wait_ttl_seconds,
            )
            self.store.put_wait_entry(entry)
            self.store.bind_wait_key(actor.user_id, idempotency_key, entry.id)
            self.store.append_wait_audit(
                WaitAuditRecord(
                    wait_id=entry.id,
                    owner_id=entry.owner_id,
                    team_id=entry.team_id,
                    idempotency_key=idempotency_key,
                    from_status=None,
                    to_status=WaitStatus.WAITING,
                    actor_id=actor.user_id,
                    action="join",
                    at=now,
                )
            )
            # 登记时额度可能可用：立即尝试放行本团队队列（FIFO 决定是否轮到它）
            self._promote_team_locked(actor.team_id, now)
            fresh = self.store.get_wait_entry(entry.id)
            assert fresh is not None
            if fresh.status is WaitStatus.ADMITTED:
                return fresh, self.store.get_env(fresh.env_id)
            return fresh, None

        return self.store.atomic(_do)

    def get_wait(self, actor: Identity, wait_id: str) -> WaitEntry:
        """查询排队结果：排队中/已放行/已取消/已超时。仅本人或管理员。"""
        def _do() -> WaitEntry:
            entry = self._get_wait_or_404(wait_id)
            self._authorize_wait(actor, entry)
            self._expire_wait_locked(entry, time.time())  # 惰性失效
            return entry

        return self.store.atomic(_do)

    def cancel_wait(self, actor: Identity, wait_id: str) -> WaitEntry:
        """主动放弃排队。仅排队中可取消；已结束返回 waitlist_not_waiting。"""
        def _do() -> WaitEntry:
            entry = self._get_wait_or_404(wait_id)
            self._authorize_wait(actor, entry)
            now = time.time()
            if self._expire_wait_locked(entry, now):
                return entry
            if entry.status is not WaitStatus.WAITING:
                raise ControlPlaneError(
                    RejectReason.WAITLIST_NOT_WAITING,
                    f"waitlist entry is {entry.status.value}",
                )
            self._finish_wait_locked(
                entry,
                WaitStatus.CANCELLED,
                actor.user_id,
                "cancel",
                now,
            )
            return entry

        return self.store.atomic(_do)

    def list_waits(self, actor: Identity) -> list[WaitEntry]:
        """列出排队记录：普通用户只看自己的（含已结束历史），管理员看全量。"""
        def _do() -> list[WaitEntry]:
            now = time.time()
            entries = list(self.store.list_wait_entries())
            for e in entries:
                self._expire_wait_locked(e, now)
            if actor.is_admin:
                return entries
            return [e for e in entries if e.owner_id == actor.user_id]

        return self.store.atomic(_do)

    def sweep_expired_waits(
        self, now: float | None = None
    ) -> list[WaitEntry]:
        """兜底任务：把所有超过有效期仍排队的记录置为失效。

        失效后受影响团队再尝试放行（前面的人超时，后面的人才能顶上）。
        """
        now = now if now is not None else time.time()

        def _do() -> list[WaitEntry]:
            affected_teams: set[str] = set()
            expired: list[WaitEntry] = []
            for entry in self.store.all_waiting_entries():
                if entry.expires_at <= now:
                    self._expire_wait_locked(entry, now)
                    expired.append(entry)
                    affected_teams.add(entry.team_id)
            for team_id in affected_teams:
                self._promote_team_locked(team_id, now)
            return expired

        return self.store.atomic(_do)

    def wait_audit_trail(self, actor: Identity, wait_id: str) -> list[WaitAuditRecord]:
        """查询某条排队记录的全生命周期留痕（含权限校验）。"""
        entry = self.get_wait(actor, wait_id)
        return self.store.wait_audit_for(entry.id)
