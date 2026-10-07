"""业务域：额度排队、自动放行、超时失效与排队权限。"""

import threading

import pytest

from app.auth import Identity
from app.errors import ControlPlaneError
from app.models import EnvStatus, QueueStatus, RejectReason
from app.service import EnvService


@pytest.fixture
def dave() -> Identity:
    return Identity(user_id="dave", team_id="team-a")


def _fill_user_quota(service, store, user, n=3, team="team-a", team_max=None):
    """申请 n 个环境，并把团队配额收紧到 team_max（默认 n），使额度成为瓶颈。"""
    store.set_team_quota(team, team_max if team_max is not None else n)
    return [service.request_env(user, f"fill-{user.user_id}-{i}") for i in range(n)]


class TestEnqueue:
    """排队：额度不足时可占位、同人同键去重、取消后再排排到队尾。"""

    def test_enqueue_when_quota_full(self, service, store, alice, bob):
        _fill_user_quota(service, store, alice)
        entry = service.enqueue(bob, "q1")
        assert entry.status is QueueStatus.WAITING
        assert entry.env_id is None
        # 查询当前状态
        got = service.get_queue_entry(bob, entry.id)
        assert got.status is QueueStatus.WAITING

    def test_enqueue_with_available_quota_promotes_immediately(self, service, alice):
        entry = service.enqueue(alice, "q-now")
        assert entry.status is QueueStatus.PROMOTED
        assert entry.env_id is not None
        env = service.get_env(alice, entry.env_id)
        assert env.status is EnvStatus.REQUESTED  # 与直接申请结果一致

    def test_enqueue_with_existing_env_is_idempotent(self, service, alice):
        env = service.request_env(alice, "q-dup-env")
        entry = service.enqueue(alice, "q-dup-env")
        assert entry.status is QueueStatus.PROMOTED
        assert entry.env_id == env.id
        assert service.usage(alice)["user"]["used"] == 1

    def test_duplicate_enqueue_same_key_counts_once(self, service, store, alice, bob):
        _fill_user_quota(service, store, alice)
        e1 = service.enqueue(bob, "q-dup")
        e2 = service.enqueue(bob, "q-dup")
        assert e1.id == e2.id  # 只占一个位置
        waiting = [e for e in service.list_queue(bob) if e.status is QueueStatus.WAITING]
        assert len(waiting) == 1

    def test_cancel_then_reenqueue_goes_to_tail(self, service, store, alice, bob, dave):
        envs = _fill_user_quota(service, store, alice)
        first = service.enqueue(bob, "q-re")
        service.enqueue(dave, "q-dave")
        service.cancel_queue_entry(bob, first.id)
        second = service.enqueue(bob, "q-re")
        assert second.id != first.id
        assert first.status is QueueStatus.CANCELLED
        # 释放一个名额：dave 排在前面先放行，bob 重排后仍在等
        service.release(alice, envs[0].id)
        entries = {e.idempotency_key: e for e in service.list_queue(bob)}
        assert entries["q-re"].status is QueueStatus.WAITING
        dave_entries = service.list_queue(dave)
        assert dave_entries[0].status is QueueStatus.PROMOTED

    def test_newcomer_queues_behind_existing_waiters(self, service, store, alice, bob, dave):
        """队里有人等时，即使团队额度有富余，新入队也排尾（不插队）。"""
        envs = _fill_user_quota(service, store, alice, team_max=6)
        bob_envs = _fill_user_quota(service, store, bob, n=3, team_max=6)  # 团队占满 6
        e_bob = service.enqueue(bob, "q-waiting")  # bob 用户配额满，排队
        service.release(alice, envs[0].id)  # 团队腾出 1 份，但 bob 用户配额满，放不了
        assert e_bob.status is QueueStatus.WAITING
        # 此时团队有富余额度，但 dave / erin 入队仍排在 bob 后面
        e_dave = service.enqueue(dave, "q-newcomer")
        e_erin = service.enqueue(Identity(user_id="erin", team_id="team-a"), "q-erin")
        assert e_dave.status is QueueStatus.WAITING
        assert e_erin.status is QueueStatus.WAITING
        # bob 释放自己的环境腾出用户配额：按顺序放行 bob、dave，erin 继续等
        service.release(bob, bob_envs[0].id)
        assert e_bob.status is QueueStatus.PROMOTED
        assert e_dave.status is QueueStatus.PROMOTED
        assert e_erin.status is QueueStatus.WAITING
        assert store.team_quota("team-a").used == 6  # 不超发


class TestAutoPromote:
    """自动放行：释放/回收触发、FIFO 顺序、不超发、与手动重试不重复建。"""

    def test_release_promotes_first_waiter(self, service, store, alice, bob):
        envs = _fill_user_quota(service, store, alice)
        entry = service.enqueue(bob, "p1")
        service.release(alice, envs[0].id)
        assert entry.status is QueueStatus.PROMOTED
        assert entry.env_id is not None
        # 放行出的环境走既有生命周期，与直接申请一致
        env = service.get_env(bob, entry.env_id)
        assert env.status is EnvStatus.REQUESTED
        service.provision(bob, env.id)
        assert env.status is EnvStatus.READY

    def test_fifo_order_is_respected(self, service, store, alice, bob, dave):
        envs = _fill_user_quota(service, store, alice)
        e_bob = service.enqueue(bob, "f-bob")
        e_dave = service.enqueue(dave, "f-dave")
        service.release(alice, envs[0].id)  # 只释放一个名额
        assert e_bob.status is QueueStatus.PROMOTED  # 先排的先拿到
        assert e_dave.status is QueueStatus.WAITING
        service.release(alice, envs[1].id)
        assert e_dave.status is QueueStatus.PROMOTED

    def test_expiry_reclaim_also_promotes(self, service, store, alice, bob):
        envs = _fill_user_quota(service, store, alice)
        for e in envs:
            service.provision(alice, e.id)
        entry = service.enqueue(bob, "p-expire")
        envs[0].expires_at = 0  # 强制到期
        service.sweep_expired()
        assert entry.status is QueueStatus.PROMOTED

    def test_user_quota_blocks_promotion(self, service, store, alice, bob):
        """轮到的人用户配额仍满时不放行，也不跳过（严格 FIFO）。"""
        envs = _fill_user_quota(service, store, alice, team_max=6)
        _fill_user_quota(service, store, bob, team_max=6)  # bob 自己也占满 3 个
        entry = service.enqueue(bob, "p-blocked")
        service.release(alice, envs[0].id)  # 团队额度有了，但 bob 用户配额满
        assert entry.status is QueueStatus.WAITING

    def test_concurrent_release_never_overpromotes(self, store):
        """多份释放同时发生时：不超发、不漏放、不重复放行。"""
        service = EnvService(store)
        store.set_team_quota("team-r", 2)
        store.set_user_quota("o1", 10)
        store.set_user_quota("o2", 10)
        o1 = Identity(user_id="o1", team_id="team-r")
        o2 = Identity(user_id="o2", team_id="team-r")
        envs = [service.request_env(o1, "r1"), service.request_env(o2, "r2")]
        waiters = [
            Identity(user_id=f"w{i}", team_id="team-r") for i in range(5)
        ]
        entries = [service.enqueue(w, f"w-key-{w.user_id}") for w in waiters]
        assert all(e.status is QueueStatus.WAITING for e in entries)

        def worker(env_id):
            service.release(o1 if env_id == envs[0].id else o2, env_id)

        threads = [threading.Thread(target=worker, args=(e.id,)) for e in envs * 5]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        promoted = [e for e in entries if e.status is QueueStatus.PROMOTED]
        waiting = [e for e in entries if e.status is QueueStatus.WAITING]
        assert len(promoted) == 2  # 只放出两个名额，不超发
        assert promoted == entries[:2]  # 严格按入队顺序，不漏不跳
        assert len(waiting) == 3
        assert len({e.env_id for e in promoted}) == 2  # 不重复放行到同一环境
        assert store.team_quota("team-r").used == 2  # 不突破团队上限

    def test_promote_and_manual_retry_create_single_env(self, service, store, alice, bob):
        """自动放行与用户同时手动重试：同键只存在一个环境。"""
        envs = _fill_user_quota(service, store, alice)
        entry = service.enqueue(bob, "race-key")
        errors = []

        def releaser():
            service.release(alice, envs[0].id)

        def retrier():
            try:
                for _ in range(5):
                    service.request_env(bob, "race-key")
            except ControlPlaneError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=releaser)] + [
            threading.Thread(target=retrier) for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert entry.status is QueueStatus.PROMOTED
        env = service.request_env(bob, "race-key")  # 幂等重放
        assert env.id == entry.env_id
        assert service.usage(bob)["user"]["used"] == 1  # 只建了一个环境


class TestQueueExpiry:
    """排队兜底：超时自动失效，终态可区分。"""

    def test_waiting_entry_expires_after_ttl(self, store, alice, bob):
        service = EnvService(store, queue_ttl_seconds=100.0)
        _fill_user_quota(service, store, alice)
        entry = service.enqueue(bob, "ttl-1")
        expired = service.sweep_queue(now=entry.expires_at + 1)
        assert [e.id for e in expired] == [entry.id]
        assert entry.status is QueueStatus.EXPIRED

    def test_not_expired_before_ttl(self, store, alice, bob):
        service = EnvService(store, queue_ttl_seconds=100.0)
        _fill_user_quota(service, store, alice)
        entry = service.enqueue(bob, "ttl-2")
        assert service.sweep_queue(now=entry.expires_at - 1) == []
        assert entry.status is QueueStatus.WAITING

    def test_terminal_statuses_are_distinguishable(self, service, store, alice, bob, dave):
        envs = _fill_user_quota(service, store, alice)
        e_wait = service.enqueue(bob, "st-wait")
        e_cancel = service.enqueue(dave, "st-cancel")
        service.cancel_queue_entry(dave, e_cancel.id)
        e_promote = service.enqueue(
            Identity(user_id="erin", team_id="team-a"), "st-promote"
        )
        service.release(alice, envs[0].id)
        assert e_wait.status is QueueStatus.PROMOTED
        assert e_cancel.status is QueueStatus.CANCELLED
        assert e_promote.status is QueueStatus.WAITING
        # 四种状态可区分：waiting / promoted / cancelled / expired
        assert {e_wait.status, e_cancel.status, e_promote.status} == {
            QueueStatus.PROMOTED,
            QueueStatus.CANCELLED,
            QueueStatus.WAITING,
        }

    def test_expired_entry_frees_position_for_reenqueue(self, store, alice, bob):
        service = EnvService(store, queue_ttl_seconds=100.0)
        _fill_user_quota(service, store, alice)
        old = service.enqueue(bob, "ttl-3")
        service.sweep_queue(now=old.expires_at + 1)
        new = service.enqueue(bob, "ttl-3")  # 失效后可重新排队
        assert new.id != old.id
        assert new.status is QueueStatus.WAITING


class TestQueuePermissions:
    """排队权限：仅本人或管理员可见可操作，同团队成员也不行。"""

    def test_owner_and_admin_only(self, service, store, alice, bob, dave, admin):
        _fill_user_quota(service, store, alice)
        entry = service.enqueue(bob, "perm-1")
        # 本人可查
        assert service.get_queue_entry(bob, entry.id).id == entry.id
        # 管理员可查、可取消
        assert service.get_queue_entry(admin, entry.id).id == entry.id
        # 同团队成员不可见不可操作
        for op in (
            lambda: service.get_queue_entry(dave, entry.id),
            lambda: service.cancel_queue_entry(dave, entry.id),
            lambda: service.queue_audit_trail(dave, entry.id),
        ):
            with pytest.raises(ControlPlaneError) as ei:
                op()
            assert ei.value.reason is RejectReason.FORBIDDEN
        service.cancel_queue_entry(admin, entry.id)
        assert entry.status is QueueStatus.CANCELLED

    def test_list_scoped_to_owner(self, service, store, alice, bob, admin):
        _fill_user_quota(service, store, alice)
        service.enqueue(bob, "list-1")
        service.enqueue(alice, "list-2")
        assert {e.owner_id for e in service.list_queue(bob)} == {"bob"}
        assert len(service.list_queue(admin)) == 2

    def test_cancel_terminal_entry_rejected(self, service, store, alice, bob):
        envs = _fill_user_quota(service, store, alice)
        entry = service.enqueue(bob, "perm-2")
        service.release(alice, envs[0].id)  # promoted
        with pytest.raises(ControlPlaneError) as ei:
            service.cancel_queue_entry(bob, entry.id)
        assert ei.value.reason is RejectReason.ILLEGAL_TRANSITION

    def test_get_missing_entry_is_not_found(self, service, alice):
        with pytest.raises(ControlPlaneError) as ei:
            service.get_queue_entry(alice, "missing")
        assert ei.value.reason is RejectReason.NOT_FOUND


class TestQueueAudit:
    """排队审计：入队/放行/取消/失效每步可追溯。"""

    def test_full_trail_is_recorded(self, service, store, alice, bob):
        envs = _fill_user_quota(service, store, alice)
        entry = service.enqueue(bob, "audit-1")
        service.release(alice, envs[0].id)
        records = service.queue_audit_trail(bob, entry.id)
        actions = [(r.action, r.from_status, r.to_status) for r in records]
        assert actions[0] == ("enqueue", None, QueueStatus.WAITING)
        assert ("promote", QueueStatus.WAITING, QueueStatus.PROMOTED) in actions
        # 放行记录可追溯时间
        promote = [r for r in records if r.action == "promote"][0]
        assert promote.at >= records[0].at

    def test_cancel_and_expire_trails(self, store, alice, bob, dave):
        service = EnvService(store, queue_ttl_seconds=50.0)
        _fill_user_quota(service, store, alice)
        e1 = service.enqueue(bob, "audit-cancel")
        service.cancel_queue_entry(bob, e1.id)
        e2 = service.enqueue(dave, "audit-expire")
        service.sweep_queue(now=e2.expires_at + 1)
        cancel_actions = [r.action for r in service.queue_audit_trail(bob, e1.id)]
        expire_actions = [r.action for r in service.queue_audit_trail(dave, e2.id)]
        assert cancel_actions == ["enqueue", "cancel"]
        assert expire_actions == ["enqueue", "expire"]
