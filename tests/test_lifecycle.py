"""业务域：生命周期状态机推进与非法迁移。"""

import pytest

from app.errors import ControlPlaneError
from app.models import EnvStatus, RejectReason


class TestLifecycle:
    """生命周期：申请中/准备中/已就绪/到期中/已回收 的合法推进。"""

    def test_full_happy_path(self, service, alice):
        env = service.request_env(alice, "k1")
        assert env.status is EnvStatus.REQUESTED
        env = service.provision(alice, env.id)
        assert env.status is EnvStatus.READY
        env = service.renew(alice, env.id)
        assert env.renewed_count == 1
        env = service.release(alice, env.id)
        assert env.status is EnvStatus.RECLAIMED

    def test_expire_path_goes_through_expiring(self, service, alice):
        env = service.request_env(alice, "k2")
        service.provision(alice, env.id)
        env.expires_at = 0  # 强制到期
        reclaimed = service.sweep_expired()
        assert [e.id for e in reclaimed] == [env.id]
        assert env.status is EnvStatus.RECLAIMED
        actions = [r.action for r in service.store.audit_for(env.id)]
        assert "expire" in actions

    def test_illegal_jump_rejected(self, service, alice):
        env = service.request_env(alice, "k3")
        # requested 不能直接 renew（仅 ready 可续期）
        with pytest.raises(ControlPlaneError) as ei:
            service.renew(alice, env.id)
        assert ei.value.reason is RejectReason.ILLEGAL_TRANSITION

    def test_renew_limit(self, service, alice):
        env = service.request_env(alice, "k4")
        service.provision(alice, env.id)
        for _ in range(3):
            service.renew(alice, env.id)
        with pytest.raises(ControlPlaneError) as ei:
            service.renew(alice, env.id)
        assert ei.value.reason is RejectReason.ILLEGAL_TRANSITION

    def test_ops_on_reclaimed_rejected_with_distinct_reason(self, service, alice):
        env = service.request_env(alice, "k5")
        service.release(alice, env.id)
        for op in (
            lambda: service.provision(alice, env.id),
            lambda: service.renew(alice, env.id),
            lambda: service.release(alice, env.id),
        ):
            with pytest.raises(ControlPlaneError) as ei:
                op()
            assert ei.value.reason is RejectReason.ALREADY_RECLAIMED

    def test_not_found(self, service, alice):
        with pytest.raises(ControlPlaneError) as ei:
            service.get_env(alice, "no-such-id")
        assert ei.value.reason is RejectReason.NOT_FOUND

    def test_audit_trail_records_every_transition(self, service, alice):
        env = service.request_env(alice, "k6")
        service.provision(alice, env.id)
        service.release(alice, env.id)
        trail = service.store.audit_for(env.id)
        transitions = [(r.from_status, r.to_status) for r in trail]
        assert transitions[0] == (None, EnvStatus.REQUESTED)
        assert (EnvStatus.REQUESTED, EnvStatus.PROVISIONING) in transitions
        assert (EnvStatus.PROVISIONING, EnvStatus.READY) in transitions
        assert transitions[-1] == (EnvStatus.READY, EnvStatus.RECLAIMED)
