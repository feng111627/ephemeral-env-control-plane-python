"""幂等与重复提交。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from app.models import EnvStatus


class TestIdempotency:
    def test_same_key_returns_same_env(self, service, alice):
        e1 = service.apply(alice, "same-key")
        e2 = service.apply(alice, "same-key")
        assert e1.env_id == e2.env_id

    def test_retry_does_not_consume_extra_quota(self, service, alice):
        service.apply(alice, "same-key")
        service.apply(alice, "same-key")
        service.apply(alice, "same-key")
        assert service.usage(alice)["user"]["active"] == 1

    def test_different_keys_create_different_envs(self, service, alice):
        e1 = service.apply(alice, "key-a")
        e2 = service.apply(alice, "key-b")
        assert e1.env_id != e2.env_id

    def test_same_key_different_users_are_independent(self, service, alice, carol):
        e1 = service.apply(alice, "shared-key")
        e2 = service.apply(carol, "shared-key")
        assert e1.env_id != e2.env_id

    def test_concurrent_same_key_apply_creates_exactly_one(self, service, alice):
        with ThreadPoolExecutor(max_workers=16) as pool:
            envs = list(pool.map(lambda _: service.apply(alice, "race-key"), range(16)))
        assert len({e.env_id for e in envs}) == 1
        assert service.usage(alice)["user"]["active"] == 1

    def test_reclaimed_env_key_still_idempotent(self, service, alice):
        """已回收环境的幂等键重试仍返回原环境，不多建、不再占配额。"""
        e1 = service.apply(alice, "k1")
        service.release(alice, e1.env_id)
        e2 = service.apply(alice, "k1")
        assert e2.env_id == e1.env_id
        assert e2.status is EnvStatus.RECLAIMED
        assert service.usage(alice)["user"]["active"] == 0
