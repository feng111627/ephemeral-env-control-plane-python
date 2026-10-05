"""审计追溯。"""
from __future__ import annotations

from app.models import EnvStatus


class TestAudit:
    def test_every_state_change_recorded(self, service, alice):
        env = service.apply(alice, "k1")
        service.renew(alice, env.env_id)
        service.release(alice, env.env_id)
        audits = service.audits(alice, env.env_id)
        transitions = [(a.from_status, a.to_status) for a in audits]
        assert (None, EnvStatus.REQUESTED) in transitions
        assert (EnvStatus.REQUESTED, EnvStatus.PROVISIONING) in transitions
        assert (EnvStatus.PROVISIONING, EnvStatus.READY) in transitions
        assert (EnvStatus.READY, EnvStatus.RECLAIMED) in transitions
        assert any(a.action == "renew" for a in audits)

    def test_audit_records_actor_and_order(self, service, alice, bob):
        env = service.apply(alice, "k1")
        service.renew(bob, env.env_id)
        audits = service.audits(alice, env.env_id)
        assert audits[0].actor_id == "alice"
        assert audits[-1].actor_id == "bob"
        assert [a.at for a in audits] == sorted(a.at for a in audits)

    def test_expire_reclaim_audited_as_system(self, service, alice):
        env = service.apply(alice, "k1", ttl_seconds=60)
        env.expires_at = 0
        service.expire_due()
        audits = service.audits(alice, env.env_id)
        reclaim = [a for a in audits if a.action == "reclaim"]
        assert len(reclaim) == 1
        assert reclaim[0].actor_id == "system"
