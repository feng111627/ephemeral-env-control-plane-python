"""配额与占用统计；并发抢配额与并发回收。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.errors import QuotaExceededError
from app.models import EnvStatus, Principal
from app.service import EnvService
from app.store import DEFAULT_TEAM_QUOTA, DEFAULT_USER_QUOTA


class TestQuota:
    def test_user_quota_exceeded_rejected(self, service, alice):
        for i in range(DEFAULT_USER_QUOTA):
            service.apply(alice, f"k{i}")
        with pytest.raises(QuotaExceededError) as ei:
            service.apply(alice, "k-overflow")
        assert ei.value.code == "quota_exceeded"
        assert "user" in ei.value.message

    def test_team_quota_exceeded_rejected(self, service):
        # 团队配额 8，用 3 个不同用户（各自用户配额 3）填满团队
        users = [Principal(f"u{i}", "team-x") for i in range(3)]
        n = 0
        for u in users:
            for j in range(DEFAULT_USER_QUOTA):
                try:
                    service.apply(u, f"k{u.user_id}-{j}")
                    n += 1
                except QuotaExceededError:
                    pass
        assert n == DEFAULT_TEAM_QUOTA
        with pytest.raises(QuotaExceededError) as ei:
            service.apply(Principal("u9", "team-x"), "k9")
        assert "team" in ei.value.message

    def test_unsettled_envs_block_new_apply(self, service, alice):
        """有未结清环境（占满配额）时新申请必须被拒绝。"""
        for i in range(DEFAULT_USER_QUOTA):
            service.apply(alice, f"k{i}")
        with pytest.raises(QuotaExceededError):
            service.apply(alice, "k-new")

    def test_quota_reusable_after_release(self, service, alice):
        envs = [service.apply(alice, f"k{i}") for i in range(DEFAULT_USER_QUOTA)]
        service.release(alice, envs[0].env_id)
        new_env = service.apply(alice, "k-reuse")
        assert new_env.status is EnvStatus.READY

    def test_quota_reusable_after_expire(self, service, alice):
        envs = [service.apply(alice, f"k{i}", ttl_seconds=60)
                for i in range(DEFAULT_USER_QUOTA)]
        for e in envs:
            e.expires_at = 0
        service.expire_due()
        assert service.usage(alice)["user"]["active"] == 0
        service.apply(alice, "k-after-expire")

    def test_usage_reports_user_and_team(self, service, alice, bob):
        service.apply(alice, "k1")
        service.apply(bob, "k2")
        usage = service.usage(alice)
        assert usage["user"] == {"user_id": "alice", "quota": DEFAULT_USER_QUOTA, "active": 1}
        assert usage["team"]["active"] == 2


class TestQuotaConcurrency:
    def test_concurrent_apply_never_exceeds_user_quota(self, service, alice):
        n = DEFAULT_USER_QUOTA * 4
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(
                lambda i: _try_apply(service, alice, f"ck{i}"), range(n)
            ))
        succeeded = [r for r in results if r is not None]
        assert len(succeeded) == DEFAULT_USER_QUOTA
        assert len({e.env_id for e in succeeded}) == DEFAULT_USER_QUOTA
        assert service.usage(alice)["user"]["active"] == DEFAULT_USER_QUOTA

    def test_concurrent_apply_never_exceeds_team_quota(self, service):
        users = [Principal(f"cu{i}", "team-c") for i in range(DEFAULT_TEAM_QUOTA * 2)]
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(
                lambda u: _try_apply(service, u, f"tk-{u.user_id}"), users
            ))
        succeeded = [r for r in results if r is not None]
        assert len(succeeded) <= DEFAULT_TEAM_QUOTA
        assert service.usage(users[0])["team"]["active"] <= DEFAULT_TEAM_QUOTA

    def test_concurrent_reclaim_frees_quota_exactly_once(self, service, alice):
        env = service.apply(alice, "k1", ttl_seconds=60)
        env.expires_at = 0
        with ThreadPoolExecutor(max_workers=8) as pool:
            sweeps = list(pool.map(lambda _: service.expire_due(), range(8)))
        total = [e for s in sweeps for e in s]
        # 同一份额度只被释放一次：全局只有一个 sweep 回收了它
        assert [e.env_id for e in total].count(env.env_id) == 1
        assert env.status is EnvStatus.RECLAIMED
        assert service.usage(alice)["user"]["active"] == 0

    def test_concurrent_release_only_one_wins(self, service, alice):
        env = service.apply(alice, "k1")
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(
                lambda _: _try_release(service, alice, env.env_id), range(8)
            ))
        assert outcomes.count("ok") == 1
        assert outcomes.count("already_reclaimed") == 7
        assert service.usage(alice)["user"]["active"] == 0

    def test_concurrent_renew_and_release_consistent(self, service, alice):
        env = service.apply(alice, "k1")
        with ThreadPoolExecutor(max_workers=8) as pool:
            pool.map(lambda f: f(), [
                *[lambda: _try(service.renew, alice, env.env_id) for _ in range(4)],
                lambda: _try(service.release, alice, env.env_id),
            ])
        # 无论时序如何，最终占用统计一致
        active = service.usage(alice)["user"]["active"]
        assert active == (0 if env.status is EnvStatus.RECLAIMED else 1)


def _try_apply(service: EnvService, p: Principal, key: str):
    try:
        return service.apply(p, key)
    except QuotaExceededError:
        return None


def _try_release(service: EnvService, p: Principal, env_id: str) -> str:
    try:
        service.release(p, env_id)
        return "ok"
    except Exception as exc:  # AlreadyReclaimedError
        return exc.code


def _try(fn, *args):
    try:
        fn(*args)
    except Exception:
        pass
