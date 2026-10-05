"""生命周期推进与非法迁移。"""
from __future__ import annotations

import pytest

from app.errors import AlreadyReclaimedError, InvalidTransitionError
from app.models import EnvStatus
from app.service import EnvService


class TestLifecycle:
    def test_apply_lands_ready_via_allowed_path(self, service, alice):
        env = service.apply(alice, "k1")
        assert env.status is EnvStatus.READY
        actions = [a.action for a in service.audits(alice, env.env_id)]
        assert actions == ["apply", "provision", "provision"]

    def test_renew_extends_expiry(self, service, alice):
        env = service.apply(alice, "k1", ttl_seconds=600)
        before = env.expires_at
        renewed = service.renew(alice, env.env_id, extend_seconds=1800)
        assert renewed.expires_at > before

    def test_renew_rejected_on_non_ready(self, service, alice, monkeypatch):
        env = service.apply(alice, "k1")
        env.status = EnvStatus.PROVISIONING  # 构造中间态
        with pytest.raises(InvalidTransitionError):
            service.renew(alice, env.env_id)

    def test_release_moves_to_reclaimed(self, service, alice):
        env = service.apply(alice, "k1")
        released = service.release(alice, env.env_id)
        assert released.status is EnvStatus.RECLAIMED

    def test_double_release_rejected_with_distinct_reason(self, service, alice):
        env = service.apply(alice, "k1")
        service.release(alice, env.env_id)
        with pytest.raises(AlreadyReclaimedError) as ei:
            service.release(alice, env.env_id)
        assert ei.value.code == "already_reclaimed"

    def test_ops_on_reclaimed_env_rejected(self, service, alice):
        env = service.apply(alice, "k1")
        service.release(alice, env.env_id)
        with pytest.raises(AlreadyReclaimedError):
            service.renew(alice, env.env_id)

    def test_illegal_transition_jump_rejected(self, service, alice):
        env = service.apply(alice, "k1")
        # READY 不能直接跳回 PROVISIONING / REQUESTED
        with pytest.raises(InvalidTransitionError):
            service._transition(env, EnvStatus.PROVISIONING, "x", "hack")
        with pytest.raises(InvalidTransitionError):
            service._transition(env, EnvStatus.REQUESTED, "x", "hack")
        assert env.status is EnvStatus.READY

    def test_expire_due_walks_ready_expiring_reclaimed(self, service, alice):
        env = service.apply(alice, "k1", ttl_seconds=60)
        env.expires_at = 0  # 强制到期
        reclaimed = service.expire_due()
        assert [e.env_id for e in reclaimed] == [env.env_id]
        assert env.status is EnvStatus.RECLAIMED
        statuses = [(a.from_status, a.to_status) for a in service.audits(alice, env.env_id)]
        assert (EnvStatus.READY, EnvStatus.EXPIRING) in statuses
        assert (EnvStatus.EXPIRING, EnvStatus.RECLAIMED) in statuses

    def test_expire_due_ignores_unexpired(self, service, alice):
        service.apply(alice, "k1", ttl_seconds=3600)
        assert service.expire_due() == []
