"""业务域：HTTP 接口端到端（错误原因可区分、既有入口不受影响）。"""

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def h_alice():
    return {"X-User-Id": "api-alice", "X-Team-Id": "api-team-a"}


@pytest.fixture
def h_carol():
    return {"X-User-Id": "api-carol", "X-Team-Id": "api-team-b"}


class TestHttpApi:
    """HTTP：状态码与 reason 字段可区分，既有根路由不变。"""

    def test_root_route_unchanged(self, client):
        assert client.get("/").json() == {"Hello": "World"}

    def test_create_and_replay_idempotent(self, client, h_alice):
        r1 = client.post("/envs", json={"idempotency_key": "http-k1"}, headers=h_alice)
        assert r1.status_code == 201
        r2 = client.post("/envs", json={"idempotency_key": "http-k1"}, headers=h_alice)
        assert r2.json()["id"] == r1.json()["id"]

    def test_error_reasons_are_distinguishable(self, client, h_alice, h_carol):
        env_id = client.post(
            "/envs", json={"idempotency_key": "http-k2"}, headers=h_alice
        ).json()["id"]
        # 越权：403 + forbidden
        r = client.get(f"/envs/{env_id}", headers=h_carol)
        assert r.status_code == 403
        assert r.json()["detail"]["reason"] == "forbidden"
        # 不存在：404 + not_found
        r = client.get("/envs/nope", headers=h_carol)
        assert r.status_code == 404
        assert r.json()["detail"]["reason"] == "not_found"
        # 非法迁移：409 + illegal_transition
        r = client.post(f"/envs/{env_id}/renew", headers=h_alice)
        assert r.status_code == 409
        assert r.json()["detail"]["reason"] == "illegal_transition"
        # 回收后操作：409 + already_reclaimed
        client.post(f"/envs/{env_id}/release", headers=h_alice)
        r = client.post(f"/envs/{env_id}/renew", headers=h_alice)
        assert r.status_code == 409
        assert r.json()["detail"]["reason"] == "already_reclaimed"

    def test_audit_endpoint(self, client, h_alice):
        env_id = client.post(
            "/envs", json={"idempotency_key": "http-k3"}, headers=h_alice
        ).json()["id"]
        client.post(f"/envs/{env_id}/provision", headers=h_alice)
        r = client.get(f"/envs/{env_id}/audit", headers=h_alice)
        assert r.status_code == 200
        actions = [rec["action"] for rec in r.json()]
        assert actions == ["request", "provision", "provision"]

    def test_sweep_endpoint(self, client, h_alice):
        env_id = client.post(
            "/envs", json={"idempotency_key": "http-k4"}, headers=h_alice
        ).json()["id"]
        client.post(f"/envs/{env_id}/provision", headers=h_alice)
        from app.api import service

        service.store.get_env(env_id).expires_at = 0
        r = client.post("/envs/sweep", headers=h_alice)
        assert any(e["id"] == env_id for e in r.json())
