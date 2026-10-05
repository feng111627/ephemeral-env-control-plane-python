"""HTTP 接口端到端：错误码可区分、既有入口不破坏。"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import build_service, create_router, install_exception_handlers


@pytest.fixture()
def client():
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(create_router(build_service()), prefix="/api")

    @app.get("/")
    def root():
        return {"Hello": "World"}

    return TestClient(app)


H_ALICE = {"X-User-Id": "alice", "X-Team-Id": "team-a"}
H_CAROL = {"X-User-Id": "carol", "X-Team-Id": "team-b"}
H_ADMIN = {"X-User-Id": "root", "X-Team-Id": "ops", "X-Role": "admin"}


class TestApi:
    def test_existing_root_behavior_preserved(self, client):
        assert client.get("/").json() == {"Hello": "World"}

    def test_full_lifecycle_over_http(self, client):
        r = client.post("/api/envs", json={"idempotency_key": "k1"}, headers=H_ALICE)
        assert r.status_code == 201
        env = r.json()
        assert env["status"] == "ready"
        assert client.post(f"/api/envs/{env['env_id']}/renew",
                           json={"extend_seconds": 600}, headers=H_ALICE).status_code == 200
        audits = client.get(f"/api/envs/{env['env_id']}/audits", headers=H_ALICE).json()
        assert [a["action"] for a in audits][:3] == ["apply", "provision", "provision"]
        rel = client.post(f"/api/envs/{env['env_id']}/release", headers=H_ALICE).json()
        assert rel["status"] == "reclaimed"

    def test_idempotent_retry_over_http(self, client):
        r1 = client.post("/api/envs", json={"idempotency_key": "k1"}, headers=H_ALICE)
        r2 = client.post("/api/envs", json={"idempotency_key": "k1"}, headers=H_ALICE)
        assert r1.json()["env_id"] == r2.json()["env_id"]
        assert client.get("/api/usage", headers=H_ALICE).json()["user"]["active"] == 1

    def test_error_codes_are_distinguishable(self, client):
        env = client.post("/api/envs", json={"idempotency_key": "k1"},
                          headers=H_ALICE).json()
        cases = {
            "forbidden": client.get(f"/api/envs/{env['env_id']}", headers=H_CAROL),
            "not_found": client.get("/api/envs/nope", headers=H_ALICE),
        }
        client.post(f"/api/envs/{env['env_id']}/release", headers=H_ALICE)
        cases["already_reclaimed"] = client.post(
            f"/api/envs/{env['env_id']}/release", headers=H_ALICE)
        for i in range(3):
            client.post("/api/envs", json={"idempotency_key": f"f{i}"}, headers=H_ALICE)
        cases["quota_exceeded"] = client.post(
            "/api/envs", json={"idempotency_key": "overflow"}, headers=H_ALICE)
        expected = {"forbidden": 403, "not_found": 404,
                    "already_reclaimed": 409, "quota_exceeded": 409}
        for code, resp in cases.items():
            assert resp.json()["error"]["code"] == code
            assert resp.status_code == expected[code]

    def test_admin_and_expire_endpoint(self, client):
        env = client.post("/api/envs", json={"idempotency_key": "k1", "ttl_seconds": 60},
                          headers=H_ALICE).json()
        # 管理员可见任意环境
        assert client.get(f"/api/envs/{env['env_id']}", headers=H_ADMIN).status_code == 200
        # 手动触发到期清扫（本地验证钩子）
        from app.models import now
        _ = now  # ttl=60 未到期，清扫应为空
        assert client.post("/api/expire", headers=H_ADMIN).json()["reclaimed"] == []
