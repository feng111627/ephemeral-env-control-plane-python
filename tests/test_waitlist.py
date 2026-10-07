"""业务域：额度紧张时的排队等候与额度释放触发的自动放行。"""

import threading
import time

import pytest

from app.auth import Identity
from app.errors import ControlPlaneError
from app.models import EnvStatus, RejectReason, WaitStatus
from app.service import EnvService
from app.store import Store


def make_service(
    user_quota: int = 3,
    team_quota: int = 8,
    wait_ttl: float = 1800.0,
) -> EnvService:
    store = Store(
        default_user_quota=user_quota, default_team_quota=team_quota
    )
    return EnvService(store, wait_ttl_seconds=wait_ttl)


def fill_user_quota(service: EnvService, user: Identity, n: int, prefix: str):
    return [service.request_env(user, f"{prefix}-{i}") for i in range(n)]


class TestJoinAndQuery:
    """排队登记与状态查询：排队中/已放行/已取消/已超时 可区分。"""

    def test_join_when_quota_full_is_waiting(self, service, alice):
        fill_user_quota(service, alice, 3, "occupy")
        entry, env = service.join_waitlist(alice, "wait-1")
        assert env is None
        assert entry.status is WaitStatus.WAITING
        assert entry.team_id == "team-a"

    def test_join_when_quota_available_admits_immediately(self, service, alice):
        entry, env = service.join_waitlist(alice, "fast-1")
        assert entry.status is WaitStatus.ADMITTED
        assert env is not None
        assert entry.env_id == env.id
        assert env.status is EnvStatus.REQUESTED
        assert service.usage(alice)["user"]["used"] == 1

    def test_repeat_join_same_key_same_position(self, service, alice):
        fill_user_quota(service, alice, 3, "occupy")
        e1, _ = service.join_waitlist(alice, "dup")
        e2, _ = service.join_waitlist(alice, "dup")
        assert e1.id == e2.id
        assert service.usage(alice)["user"]["used"] == 3

    def test_join_with_key_that_has_env_rejected(self, service, alice):
        service.request_env(alice, "used-key")
        with pytest.raises(ControlPlaneError) as ei:
            service.join_waitlist(alice, "used-key")
        assert ei.value.reason is RejectReason.ALREADY_REQUESTED

    def test_statuses_are_distinguishable(self, service, alice):
        fill_user_quota(service, alice, 3, "occupy")
        waiting, _ = service.join_waitlist(alice, "w")
        got = service.get_wait(alice, waiting.id)
        assert got.status is WaitStatus.WAITING

        cancelled, _ = service.join_waitlist(alice, "c")
        service.cancel_wait(alice, cancelled.id)
        assert service.get_wait(alice, cancelled.id).status is WaitStatus.CANCELLED

        expired, _ = service.join_waitlist(alice, "e")
        expired.expires_at = 0
        assert service.get_wait(alice, expired.id).status is WaitStatus.EXPIRED


class TestAutoAdmit:
    """核心：额度释放（主动释放/到期回收）自动按序放行。"""

    def test_active_release_admits_head_of_queue(self, service, alice):
        envs = fill_user_quota(service, alice, 3, "occupy")
        w1, _ = service.join_waitlist(alice, "w1")
        w2, _ = service.join_waitlist(alice, "w2")
        service.release(alice, envs[0].id)
        # 一次释放只放行一个，且先排先得
        assert service.get_wait(alice, w1.id).status is WaitStatus.ADMITTED
        assert service.get_wait(alice, w2.id).status is WaitStatus.WAITING
        # 放行产生的环境走正常生命周期：可 provision/renew/release
        env1 = service.store.get_env(w1.env_id)
        assert env1 is not None
        ready = service.provision(alice, env1.id)
        assert ready.status is EnvStatus.READY
        assert service.usage(alice)["user"]["used"] == 3

    def test_fifo_order_across_users_in_same_team(self, store, service, alice, bob):
        # 团队配额 2，制造跨用户排队
        store.set_team_quota("team-a", 2)
        service.request_env(alice, "a0")
        service.request_env(bob, "b0")
        wb, _ = service.join_waitlist(bob, "bw")
        wa, _ = service.join_waitlist(alice, "aw")
        # bob 先排 -> 先放；alice 仍等待
        envs = service.store.list_envs()
        service.release(alice, envs[0].id)
        assert service.get_wait(bob, wb.id).status is WaitStatus.ADMITTED
        assert service.get_wait(alice, wa.id).status is WaitStatus.WAITING

    def test_multiple_releases_admit_multiple_waiters_without_overshoot(
        self, service, alice
    ):
        envs = fill_user_quota(service, alice, 3, "occupy")
        waits = [service.join_waitlist(alice, f"w{i}")[0] for i in range(5)]
        service.release(alice, envs[0].id)
        service.release(alice, envs[1].id)
        admitted = [
            w for w in waits
            if service.get_wait(alice, w.id).status is WaitStatus.ADMITTED
        ]
        assert len(admitted) == 2
        assert service.usage(alice)["user"]["used"] == 3  # 不超上限

    def test_expire_reclaim_also_admits(self, service, alice):
        envs = fill_user_quota(service, alice, 3, "occupy")
        for e in envs:
            service.provision(alice, e.id)
        w, _ = service.join_waitlist(alice, "w-exp")
        envs[0].expires_at = 0  # 仅一个到期，其余就绪环境仍在 TTL 内
        service.sweep_expired(now=1)
        assert service.get_wait(alice, w.id).status is WaitStatus.ADMITTED
        assert service.usage(alice)["user"]["used"] == 3

    def test_admitted_result_identical_to_direct_request(self, service, alice):
        envs = fill_user_quota(service, alice, 3, "occupy")
        w, _ = service.join_waitlist(alice, "same-key")
        service.release(alice, envs[0].id)
        env = service.store.get_env(w.env_id)
        assert env is not None
        # 与直接申请一致：幂等键绑定、初始状态 requested、有 request 审计
        assert env.idempotency_key == "same-key"
        assert service.store.find_by_idempotency(alice.user_id, "same-key").id == env.id
        actions = [r.action for r in service.store.audit_for(env.id)]
        assert actions[0] == "request"


class TestCancelAndRequeue:
    """主动放弃排队，以及取消/失效后重新排到队尾。"""

    def test_cancel_then_status_cancelled(self, service, alice):
        fill_user_quota(service, alice, 3, "occupy")
        w, _ = service.join_waitlist(alice, "cw")
        cancelled = service.cancel_wait(alice, w.id)
        assert cancelled.status is WaitStatus.CANCELLED
        # 再次取消已结束记录 -> waitlist_not_waiting
        with pytest.raises(ControlPlaneError) as ei:
            service.cancel_wait(alice, w.id)
        assert ei.value.reason is RejectReason.WAITLIST_NOT_WAITING

    def test_cancelled_does_not_consume_released_quota(self, service, alice):
        envs = fill_user_quota(service, alice, 3, "occupy")
        wc, _ = service.join_waitlist(alice, "cancel-me")
        wn, _ = service.join_waitlist(alice, "next")
        service.cancel_wait(alice, wc.id)
        service.release(alice, envs[0].id)
        assert service.get_wait(alice, wc.id).status is WaitStatus.CANCELLED
        # 释放的额度直接给到队首仍有效的 wn，wc 不会被放行
        assert service.get_wait(alice, wn.id).status is WaitStatus.ADMITTED

    def test_requeue_after_cancel_goes_to_tail(self, service, alice):
        envs = fill_user_quota(service, alice, 3, "occupy")
        first, _ = service.join_waitlist(alice, "k-x")  # 先排
        service.cancel_wait(alice, first.id)
        second, _ = service.join_waitlist(alice, "k-y")  # 别人排上
        requeued, _ = service.join_waitlist(alice, "k-x")  # 同键再排 -> 队尾
        assert requeued.id != first.id
        service.release(alice, envs[0].id)
        assert service.get_wait(alice, second.id).status is WaitStatus.ADMITTED
        assert service.get_wait(alice, requeued.id).status is WaitStatus.WAITING

    def test_requeue_after_expiry_goes_to_tail(self, service, alice):
        fill_user_quota(service, alice, 3, "occupy")
        first, _ = service.join_waitlist(alice, "k-exp")
        first.expires_at = 0
        service.sweep_expired_waits(now=1)
        again, env = service.join_waitlist(alice, "k-exp")
        assert env is None  # 额度仍满，继续排队
        assert again.id != first.id
        assert again.status is WaitStatus.WAITING


class TestWaitExpiry:
    """排队兜底：超时失效，释放后续位置，且状态可区分。"""

    def test_sweep_expires_overdue_entries(self, service, alice):
        fill_user_quota(service, alice, 3, "occupy")
        w, _ = service.join_waitlist(alice, "old")
        w.expires_at = 0
        expired = service.sweep_expired_waits(now=1)
        assert [e.id for e in expired] == [w.id]
        assert service.get_wait(alice, w.id).status is WaitStatus.EXPIRED

    def test_expired_head_lets_next_waiter_through(self, service, alice):
        envs = fill_user_quota(service, alice, 3, "occupy")
        stale, _ = service.join_waitlist(alice, "stale")
        fresh, _ = service.join_waitlist(alice, "fresh")
        stale.expires_at = 0
        service.sweep_expired_waits(now=1)
        service.release(alice, envs[0].id)
        assert service.get_wait(alice, stale.id).status is WaitStatus.EXPIRED
        assert service.get_wait(alice, fresh.id).status is WaitStatus.ADMITTED

    def test_lazy_expiry_on_get(self, service, alice):
        fill_user_quota(service, alice, 3, "occupy")
        w, _ = service.join_waitlist(alice, "lazy")
        w.expires_at = 0
        assert service.get_wait(alice, w.id).status is WaitStatus.EXPIRED


class TestWaitIsolation:
    """排队、查看、取消按身份隔离；管理员例外；同键跨用户独立。"""

    def test_outsider_cannot_view_or_cancel(self, service, alice, carol):
        fill_user_quota(service, alice, 3, "occupy")
        w, _ = service.join_waitlist(alice, "secret")
        with pytest.raises(ControlPlaneError) as ei:
            service.get_wait(carol, w.id)
        assert ei.value.reason is RejectReason.FORBIDDEN
        with pytest.raises(ControlPlaneError) as ei:
            service.cancel_wait(carol, w.id)
        assert ei.value.reason is RejectReason.FORBIDDEN

    def test_teammate_cannot_touch_wait_entry(self, service, alice, bob):
        # 排队记录比环境更私密：同团队成员也不能动别人的排队位置
        fill_user_quota(service, alice, 3, "occupy")
        w, _ = service.join_waitlist(alice, "mine")
        with pytest.raises(ControlPlaneError) as ei:
            service.cancel_wait(bob, w.id)
        assert ei.value.reason is RejectReason.FORBIDDEN

    def test_admin_can_view_and_cancel_any(self, service, alice, admin):
        fill_user_quota(service, alice, 3, "occupy")
        w, _ = service.join_waitlist(alice, "adm")
        assert service.get_wait(admin, w.id).owner_id == "alice"
        assert service.cancel_wait(admin, w.id).status is WaitStatus.CANCELLED

    def test_list_scoped_to_owner_admin_sees_all(self, service, alice, bob, admin):
        fill_user_quota(service, alice, 3, "occupy")
        service.join_waitlist(alice, "a-w")
        service.join_waitlist(bob, "b-w")
        assert {e.owner_id for e in service.list_waits(alice)} == {"alice"}
        owners = {e.owner_id for e in service.list_waits(admin)}
        assert owners == {"alice", "bob"}

    def test_same_key_different_users_independent(self, store, service, alice, carol):
        store.set_user_quota("carol", 3)
        fill_user_quota(service, alice, 3, "occupy")
        fill_user_quota(service, carol, 3, "c-occupy")
        wa, _ = service.join_waitlist(alice, "shared")
        wc, _ = service.join_waitlist(carol, "shared")
        assert wa.id != wc.id


class TestWaitAudit:
    """排队全生命周期留痕，事后可查。"""

    def test_audit_records_join_admit(self, service, alice):
        envs = fill_user_quota(service, alice, 3, "occupy")
        w, _ = service.join_waitlist(alice, "aud-1")
        service.release(alice, envs[0].id)
        trail = service.wait_audit_trail(alice, w.id)
        actions = [(r.from_status, r.to_status, r.action) for r in trail]
        assert actions[0] == (None, WaitStatus.WAITING, "join")
        assert (WaitStatus.WAITING, WaitStatus.ADMITTED, "admit_auto") in actions

    def test_audit_records_cancel(self, service, alice):
        fill_user_quota(service, alice, 3, "occupy")
        w, _ = service.join_waitlist(alice, "aud-2")
        service.cancel_wait(alice, w.id)
        actions = [r.action for r in service.wait_audit_trail(alice, w.id)]
        assert actions == ["join", "cancel"]

    def test_audit_records_expiry(self, service, alice):
        fill_user_quota(service, alice, 3, "occupy")
        w, _ = service.join_waitlist(alice, "aud-3")
        w.expires_at = 0
        service.sweep_expired_waits(now=1)
        actions = [r.action for r in service.wait_audit_trail(alice, w.id)]
        assert actions == ["join", "expire_wait"]


class TestWaitConcurrency:
    """并发：多份额度同时释放不超发、不重放、不漏放；排队与手动申请竞争不多建。"""

    def test_concurrent_releases_admit_without_overshoot(self):
        # 3 个占用同时释放，5 人排队：恰好放行 3 个、队尾 2 个继续等、配额不超限
        # 约束维度为团队配额=3：每释放 1 份只能放行 1 人
        svc = make_service(user_quota=100, team_quota=3)
        user_holder = Identity("holder", "team-c")
        occupied = fill_user_quota(svc, user_holder, 3, "hold")
        waiters = [Identity(f"w{i}", "team-c") for i in range(5)]
        entries = [svc.join_waitlist(u, f"wk{i}")[0] for i, u in enumerate(waiters)]
        errors: list[Exception] = []

        def release_worker(env):
            try:
                svc.release(user_holder, env.id)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=release_worker, args=(e,)) for e in occupied]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        statuses = [svc.get_wait(u, entries[i].id).status for i, u in enumerate(waiters)]
        assert statuses.count(WaitStatus.ADMITTED) == 3
        assert statuses.count(WaitStatus.WAITING) == 2
        # 团队维度占用：原 holder 已清零 + 新放行 3
        assert svc.store.team_quota("team-c").used == 3
        # 每个被放行的排队者只建了一个环境（无重复放行）
        admitted_env_ids = [
            svc.get_wait(u, entries[i].id).env_id for i, u in enumerate(waiters)
        ]
        admitted_env_ids = [x for x in admitted_env_ids if x]
        assert len(set(admitted_env_ids)) == 3

    def test_concurrent_releases_fifo_no_one_skipped(self):
        # 大量并发释放，验证先排先得、无遗漏
        svc = make_service(user_quota=10, team_quota=100)
        holder = Identity("holder", "team-d")
        occupied = fill_user_quota(svc, holder, 10, "hold")
        waiters = [Identity(f"q{i}", "team-d") for i in range(10)]
        entries = [svc.join_waitlist(u, f"qk{i}")[0] for i, u in enumerate(waiters)]

        barrier = threading.Barrier(11)

        def release_worker(env):
            barrier.wait()
            svc.release(holder, env.id)

        threads = [threading.Thread(target=release_worker, args=(e,)) for e in occupied]
        for t in threads:
            t.start()
        barrier.wait()
        for t in threads:
            t.join()

        for i, u in enumerate(waiters):
            assert svc.get_wait(u, entries[i].id).status is WaitStatus.ADMITTED, i
        assert svc.store.team_quota("team-d").used == 10

    def test_concurrent_join_same_key_one_position(self):
        svc = make_service(user_quota=1, team_quota=100)
        holder = Identity("holder", "team-e")
        svc.request_env(holder, "h")
        user = Identity("joinr", "team-e")
        results: list = []
        errors: list[ControlPlaneError] = []

        def worker():
            try:
                results.append(svc.join_waitlist(user, "same-wait-key")[0])
            except ControlPlaneError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len({e.id for e in results}) == 1
        # 其余并发重复排队要么返回同一条记录，要么被幂等冲突拒绝，绝不产生第二个位置
        assert len(results) + len(errors) == 16
        assert all(
            e.reason in (RejectReason.ALREADY_REQUESTED,) for e in errors
        )

    def test_auto_admit_racing_with_manual_request_creates_one_env(self):
        # 自动放行与本人同一时刻手动重试（同一幂等键）竞争：只允许一个环境
        svc = make_service(user_quota=1, team_quota=100)
        holder = Identity("holder", "team-f")
        occupied = svc.request_env(holder, "h")
        waiter = Identity("wl", "team-f")
        entry, _ = svc.join_waitlist(waiter, "race-key")
        outcomes: list = []

        barrier = threading.Barrier(2)

        def releaser():
            barrier.wait()
            svc.release(holder, occupied.id)  # 触发自动放行

        def manual_retry():
            barrier.wait()
            try:
                outcomes.append(("manual", svc.request_env(waiter, "race-key")))
            except ControlPlaneError as exc:
                outcomes.append(("rejected", exc))

        threads = [threading.Thread(target=releaser), threading.Thread(target=manual_retry)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 最终：恰好一个环境绑定该幂等键，排队记录已放行，用户占用为 1
        bound = svc.store.find_by_idempotency("wl", "race-key")
        assert bound is not None
        assert svc.store.user_quota("wl").used == 1
        fresh = svc.get_wait(waiter, entry.id)
        assert fresh.status is WaitStatus.ADMITTED
        assert fresh.env_id == bound.id
        # 若手动重试被配额拒绝，则自动放行必须成功建出环境；反之亦然，二者结果收敛
        assert outcomes  # 手动线程确实执行过
