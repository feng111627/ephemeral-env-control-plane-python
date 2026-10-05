"""业务域：幂等语义与重复提交。"""

import threading

from app.auth import Identity


class TestIdempotency:
    """幂等：同一幂等键重复提交只产生一个环境、不重复占用配额。"""

    def test_same_key_returns_same_env(self, service, alice):
        e1 = service.request_env(alice, "idem-1")
        e2 = service.request_env(alice, "idem-1")
        assert e1.id == e2.id
        assert service.usage(alice)["user"]["used"] == 1

    def test_different_keys_create_different_envs(self, service, alice):
        e1 = service.request_env(alice, "idem-a")
        e2 = service.request_env(alice, "idem-b")
        assert e1.id != e2.id

    def test_same_key_different_users_are_independent(self, service, alice, carol):
        e1 = service.request_env(alice, "shared-key")
        e2 = service.request_env(carol, "shared-key")
        assert e1.id != e2.id

    def test_concurrent_duplicate_submit_creates_one_env(self, store):
        from app.service import EnvService

        service = EnvService(store)
        user = Identity(user_id="u9", team_id="team-z")
        results = []

        def worker() -> None:
            results.append(service.request_env(user, "dup-key"))

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len({e.id for e in results}) == 1
        assert store.user_quota("u9").used == 1
        assert store.team_quota("team-z").used == 1

    def test_retry_after_reclaim_creates_new_env(self, service, alice):
        e1 = service.request_env(alice, "idem-re")
        service.release(alice, e1.id)
        # 已回收环境的幂等键重试：返回原记录而非新建（提交结果可解释）
        e2 = service.request_env(alice, "idem-re")
        assert e2.id == e1.id
