"""业务域：配额限制、并发抢配额、并发回收、额度复用。"""

import threading

from app.auth import Identity
from app.errors import ControlPlaneError
from app.models import RejectReason


class TestQuota:
    """配额：按用户/团队维度的占用统计与超限拒绝。"""

    def test_user_quota_exceeded(self, service, alice):
        for i in range(3):
            service.request_env(alice, f"k{i}")
        try:
            service.request_env(alice, "k-overflow")
            raise AssertionError("should be rejected")
        except ControlPlaneError as exc:
            assert exc.reason is RejectReason.QUOTA_EXCEEDED

    def test_team_quota_exceeded(self, service, store):
        store.set_team_quota("team-x", 2)
        store.set_user_quota("u1", 10)
        store.set_user_quota("u2", 10)
        u1 = Identity(user_id="u1", team_id="team-x")
        u2 = Identity(user_id="u2", team_id="team-x")
        service.request_env(u1, "a")
        service.request_env(u2, "b")
        try:
            service.request_env(u1, "c")
            raise AssertionError("should be rejected")
        except ControlPlaneError as exc:
            assert exc.reason is RejectReason.QUOTA_EXCEEDED

    def test_reclaimed_quota_is_reusable(self, service, alice):
        envs = [service.request_env(alice, f"k{i}") for i in range(3)]
        service.release(alice, envs[0].id)
        # 回收后额度可再用于新申请
        env = service.request_env(alice, "k-new")
        assert env.id not in {e.id for e in envs}
        usage = service.usage(alice)
        assert usage["user"]["used"] == 3


class TestConcurrency:
    """并发：并发申请不超配额、并发回收只释放一次额度。"""

    def test_concurrent_requests_never_exceed_quota(self, store):
        service = self._make_service(store)
        user = Identity(user_id="rush", team_id="team-c")
        results, errors = [], []

        def worker(i: int) -> None:
            try:
                results.append(service.request_env(user, f"race-{i}"))
            except ControlPlaneError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 3  # 用户配额上限
        assert len({e.id for e in results}) == 3
        assert all(e.reason is RejectReason.QUOTA_EXCEEDED for e in errors)
        assert store.user_quota("rush").used == 3

    def test_concurrent_reclaim_releases_quota_once(self, store, service, alice):
        env = service.request_env(alice, "k-rel")
        service.provision(alice, env.id)
        errors = []

        def worker() -> None:
            try:
                service.release(alice, env.id)
            except ControlPlaneError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert store.user_quota(alice.user_id).used == 0
        assert store.team_quota(alice.team_id).used == 0
        # 只有一次释放成功，其余都是 ALREADY_RECLAIMED
        assert len(errors) == 9
        assert all(e.reason is RejectReason.ALREADY_RECLAIMED for e in errors)

    def test_concurrent_sweep_releases_quota_once(self, store, service, alice):
        env = service.request_env(alice, "k-sweep")
        service.provision(alice, env.id)
        env.expires_at = 0
        outcomes = []

        def worker() -> None:
            outcomes.extend(service.sweep_expired())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(outcomes) == 1  # 只被一个 sweeper 回收
        assert store.user_quota(alice.user_id).used == 0

    @staticmethod
    def _make_service(store):
        from app.service import EnvService

        return EnvService(store)
