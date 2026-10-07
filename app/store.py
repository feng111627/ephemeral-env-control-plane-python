"""本地线程安全存储：环境、幂等键、配额占用、审计日志。

单进程内存实现，所有公开方法在内部可重入锁保护下执行，
保证并发申请/续期/释放/回收时的原子性。
"""
from __future__ import annotations

import threading
from collections.abc import Callable

from app.models import AuditRecord, Environment, EnvStatus, Quota, WaitAuditRecord, WaitEntry, WaitStatus

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
        # 排队等候
        self._wait_seq = 0
        self._wait_entries: dict[str, WaitEntry] = {}
        self._wait_by_key: dict[tuple[str, str], str] = {}  # (owner,key) -> wait_id（占位中）
        self._wait_audit: list[WaitAuditRecord] = []

    # ---- 通用原子执行 ----

    def atomic[T](self, fn: Callable[[], T]) -> T:
        """在全局锁内执行一段业务逻辑，保证多步读写的原子性。"""
        with self._lock:
            return fn()

    def reset(self) -> None:
        """清空全部数据（测试隔离用），保留配额默认配置。"""
        with self._lock:
            self._envs = {}
            self._idempotency = {}
            self._user_quota = {}
            self._team_quota = {}
            self._audit = []
            self._wait_seq = 0
            self._wait_entries = {}
            self._wait_by_key = {}
            self._wait_audit = []

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

    # ---- 排队等候（存储原语，调用方需持锁/在 atomic 内） ----

    def next_wait_seq(self) -> int:
        self._wait_seq += 1
        return self._wait_seq

    def put_wait_entry(self, entry: WaitEntry) -> None:
        self._wait_entries[entry.id] = entry

    def get_wait_entry(self, wait_id: str) -> WaitEntry | None:
        return self._wait_entries.get(wait_id)

    def find_waiting_by_key(self, owner_id: str, key: str) -> WaitEntry | None:
        """返回该 (owner,key) 当前占位（排队中）的记录；无占位返回 None。"""
        wait_id = self._wait_by_key.get((owner_id, key))
        return self._wait_entries.get(wait_id) if wait_id else None

    def bind_wait_key(self, owner_id: str, key: str, wait_id: str) -> None:
        self._wait_by_key[(owner_id, key)] = wait_id

    def unbind_wait_key(self, owner_id: str, key: str) -> None:
        self._wait_by_key.pop((owner_id, key), None)

    def waiting_entries_for_team(self, team_id: str) -> list[WaitEntry]:
        """团队内仍在排队的记录，按入队先后（seq）排序。"""
        entries = [
            e
            for e in self._wait_entries.values()
            if e.status is WaitStatus.WAITING and e.team_id == team_id
        ]
        entries.sort(key=lambda e: e.seq)
        return entries

    def all_waiting_entries(self) -> list[WaitEntry]:
        entries = [e for e in self._wait_entries.values() if e.status is WaitStatus.WAITING]
        entries.sort(key=lambda e: e.seq)
        return entries

    def list_wait_entries(self) -> list[WaitEntry]:
        """全部排队记录（含已结束），按入队先后排序。"""
        entries = list(self._wait_entries.values())
        entries.sort(key=lambda e: e.seq)
        return entries

    def append_wait_audit(self, record: WaitAuditRecord) -> None:
        self._wait_audit.append(record)

    def wait_audit_for(self, wait_id: str) -> list[WaitAuditRecord]:
        return [r for r in self._wait_audit if r.wait_id == wait_id]

    def list_wait_audit(self) -> list[WaitAuditRecord]:
        return list(self._wait_audit)
