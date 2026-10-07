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


class TestQueueHttpApi:
    """HTTP：排队接口端到端（排队/查询/取消/自动放行/权限隔离）。"""

    @pytest.fixture
    def h_q(self):
        return {"X-User-Id": "q-user", "X-Team-Id": "q-team"}

    @pytest.fixture
    def h_outsider(self):
        return {"X-User-Id": "q-outsider", "X-Team-Id": "q-team"}

    @pytest.fixture
    def h_admin(self):
        return {"X-User-Id": "q-admin", "X-Role": "admin"}

    def _fill_user(self, client, h):
        ids = []
        for i in range(3):  # 默认用户配额 3
            r = client.post("/envs", json={"idempotency_key": f"q-fill-{i}"}, headers=h)
            assert r.status_code == 201
            ids.append(r.json()["id"])
        return ids

    def test_enqueue_wait_and_auto_promote(self, client, h_q):
        env_ids = self._fill_user(client, h_q)
        # 额度满：排队而不是直接失败
        r = client.post("/envs/queue", json={"idempotency_key": "q-w1"}, headers=h_q)
        assert r.status_code == 201
        entry = r.json()
        assert entry["status"] == "waiting"
        # 同键重复排队只占一个位置
        r2 = client.post("/envs/queue", json={"idempotency_key": "q-w1"}, headers=h_q)
        assert r2.json()["id"] == entry["id"]
        # 查询状态
        r3 = client.get(f"/envs/queue/{entry['id']}", headers=h_q)
        assert r3.json()["status"] == "waiting"
        # 释放额度后自动放行，无需再次手动申请
        client.post(f"/envs/{env_ids[0]}/release", headers=h_q)
        r4 = client.get(f"/envs/queue/{entry['id']}", headers=h_q)
        assert r4.json()["status"] == "promoted"
        env_id = r4.json()["env_id"]
        # 放行出的环境走既有生命周期
        r5 = client.post(f"/envs/{env_id}/provision", headers=h_q)
        assert r5.json()["status"] == "ready"
        # 审计可追溯：enqueue + promote
        ra = client.get(f"/envs/queue/{entry['id']}/audit", headers=h_q)
        assert [x["action"] for x in ra.json()] == ["enqueue", "promote"]

    def test_cancel_and_reenqueue(self, client, h_q):
        r = client.post("/envs/queue", json={"idempotency_key": "q-c1"}, headers=h_q)
        entry_id = r.json()["id"]
        r2 = client.post(f"/envs/queue/{entry_id}/cancel", headers=h_q)
        assert r2.json()["status"] == "cancelled"
        # 取消后可重新排队（新位置）
        r3 = client.post("/envs/queue", json={"idempotency_key": "q-c1"}, headers=h_q)
        assert r3.json()["id"] != entry_id

    def test_queue_permissions(self, client, h_q, h_outsider, h_admin):
        r = client.post("/envs/queue", json={"idempotency_key": "q-p1"}, headers=h_q)
        entry_id = r.json()["id"]
        # 同团队其他成员不可见不可操作
        assert client.get(f"/envs/queue/{entry_id}", headers=h_outsider).status_code == 403
        assert (
            client.post(f"/envs/queue/{entry_id}/cancel", headers=h_outsider).status_code
            == 403
        )
        # 管理员可见
        assert client.get(f"/envs/queue/{entry_id}", headers=h_admin).status_code == 200
        # 列表只含本人的记录
        mine = client.get("/envs/queue", headers=h_outsider).json()
        assert all(e["owner_id"] == "q-outsider" for e in mine)
        # 不存在的记录
        assert client.get("/envs/queue/nope", headers=h_q).status_code == 404
