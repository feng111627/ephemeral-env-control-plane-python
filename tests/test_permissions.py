"""业务域：按身份与归属的操作权限。"""

import pytest

from app.errors import ControlPlaneError
from app.models import RejectReason


class TestPermissions:
    """权限：本人/同团队/管理员可见可操作，其余拒绝且原因可区分。"""

    def test_owner_can_operate(self, service, alice):
        env = service.request_env(alice, "p1")
        assert service.get_env(alice, env.id).id == env.id

    def test_teammate_can_operate(self, service, alice, bob):
        env = service.request_env(alice, "p2")
        service.provision(bob, env.id)  # 同团队成员可操作
        assert service.get_env(bob, env.id).id == env.id

    def test_admin_can_operate_any(self, service, alice, admin):
        env = service.request_env(alice, "p3")
        service.provision(admin, env.id)
        service.release(admin, env.id)

    def test_outsider_forbidden_even_if_exists(self, service, alice, carol):
        env = service.request_env(alice, "p4")
        for op in (
            lambda: service.get_env(carol, env.id),
            lambda: service.provision(carol, env.id),
            lambda: service.renew(carol, env.id),
            lambda: service.release(carol, env.id),
            lambda: service.audit_trail(carol, env.id),
        ):
            with pytest.raises(ControlPlaneError) as ei:
                op()
            assert ei.value.reason is RejectReason.FORBIDDEN

    def test_forbidden_distinct_from_not_found(self, service, alice, carol):
        env = service.request_env(alice, "p5")
        with pytest.raises(ControlPlaneError) as e_forbidden:
            service.get_env(carol, env.id)
        with pytest.raises(ControlPlaneError) as e_notfound:
            service.get_env(carol, "missing-id")
        assert e_forbidden.value.reason is RejectReason.FORBIDDEN
        assert e_notfound.value.reason is RejectReason.NOT_FOUND

    def test_list_scoped_by_identity(self, service, alice, bob, carol, admin):
        service.request_env(alice, "l1")
        service.request_env(carol, "l2")
        assert len(service.list_envs(alice)) == 1  # 自己团队
        assert len(service.list_envs(bob)) == 1
        assert len(service.list_envs(carol)) == 1
        assert len(service.list_envs(admin)) == 2  # 管理员全量
