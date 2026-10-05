"""越权访问与权限判定。"""
from __future__ import annotations

import pytest

from app.errors import ForbiddenError, NotFoundError
from app.models import EnvStatus


class TestPermission:
    def test_owner_can_operate(self, service, alice):
        env = service.apply(alice, "k1")
        assert service.get(alice, env.env_id).env_id == env.env_id
        service.renew(alice, env.env_id)
        service.release(alice, env.env_id)

    def test_same_team_can_view_and_operate(self, service, alice, bob):
        env = service.apply(alice, "k1")
        assert service.get(bob, env.env_id).env_id == env.env_id
        service.renew(bob, env.env_id)

    def test_cross_team_forbidden_with_distinct_reason(self, service, alice, carol):
        env = service.apply(alice, "k1")
        with pytest.raises(ForbiddenError) as ei:
            service.get(carol, env.env_id)
        assert ei.value.code == "forbidden"

    def test_forbidden_distinct_from_not_found(self, service, alice, carol):
        env = service.apply(alice, "k1")
        # 目标存在但越权 -> 403 forbidden
        with pytest.raises(ForbiddenError):
            service.get(carol, env.env_id)
        # 目标不存在 -> 404 not_found，两者可区分
        with pytest.raises(NotFoundError) as ei:
            service.get(carol, "no-such-env")
        assert ei.value.code == "not_found"

    def test_cross_team_cannot_release_or_renew(self, service, alice, carol):
        env = service.apply(alice, "k1")
        with pytest.raises(ForbiddenError):
            service.renew(carol, env.env_id)
        with pytest.raises(ForbiddenError):
            service.release(carol, env.env_id)
        assert env.status is EnvStatus.READY

    def test_cross_team_cannot_read_audits(self, service, alice, carol):
        env = service.apply(alice, "k1")
        with pytest.raises(ForbiddenError):
            service.audits(carol, env.env_id)

    def test_admin_can_operate_any_env(self, service, alice, admin):
        env = service.apply(alice, "k1")
        assert service.get(admin, env.env_id).env_id == env.env_id
        service.release(admin, env.env_id)
        assert env.status is EnvStatus.RECLAIMED

    def test_list_visible_scoped_by_identity(self, service, alice, bob, carol, admin):
        service.apply(alice, "k1")
        service.apply(bob, "k2")
        service.apply(carol, "k3")
        assert len(service.list_visible(alice)) == 2   # 同团队两台
        assert len(service.list_visible(carol)) == 1   # 仅自己
        assert len(service.list_visible(admin)) == 3   # 管理员全量
