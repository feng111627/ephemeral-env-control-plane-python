"""业务域：排队等候能力的 HTTP 端到端验证。"""

import pytest
from fastapi.testclient import TestClient

from main import app
from app.api import service as global_service


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _fresh_global_store():
    """每个用例在全局单例上原地清空，避免跨文件/跨用例状态污染。"""
    global_service.store.reset()
    yield
    global_service.store.reset()


@pytest.fixture(autouse=True)
def _tight_team_quota():
    # 团队配额收紧到 2，便于用两个不同用户制造排队
    global_service.store.set_team_quota("http-team-a", 2)
    yield


@pytest.fixture
def h_a1():
    return {"X-User-Id": "http-a1", "X-Team-Id": "http-team-a"}


@pytest.fixture
def h_a2():
    return {"X-User-Id": "http-a2", "X-Team-Id": "http-team-a"}


@pytest.fixture
def h_other():
    return {"X-User-Id": "http-other", "X-Team-Id": "http-team-b"}


@pytest.fixture
def h_admin():
    return {"X-User-Id": "http-root", "X-Role": "admin"}


def _join(client, headers, key):
    return client.post("/envs/waits", json={"idempotency_key": key}, headers=headers)


class TestWaitHttpApi:
    """HTTP：排队登记/查询/取消/放行/审计 与既有接口共存。"""

    def test_join_with_free_quota_admits_immediately(self, client, h_a1):
        r = _join(client, h_a1, "wh-1")
        assert r.status_code == 201
        body = r.json()
        assert body["wait"]["status"] == "admitted"
        assert body["env"] is not None
        assert body["wait"]["env_id"] == body["env"]["id"]

    def test_full_wait_release_admit_flow(self, client, h_a1, h_a2):
        # 占满团队配额
        e1 = client.post("/envs", json={"idempotency_key": "occ-1"}, headers=h_a1)
        client.post("/envs", json={"idempotency_key": "occ-2"}, headers=h_a2)
        # 第三人排队
        r = _join(client, h_a1, "wh-line")
        assert r.status_code == 201
        wait_id = r.json()["wait"]["id"]
        assert r.json()["wait"]["status"] == "waiting"
        assert r.json()["env"] is None
        # 释放一份 -> 自动放行，无需本人确认
        client.post(f"/envs/{e1.json()['id']}/release", headers=h_a1)
        g = client.get(f"/envs/waits/{wait_id}", headers=h_a1)
        assert g.json()["status"] == "admitted"
        assert g.json()["env_id"] is not None
        # 被放行的环境走既有生命周期
        prov = client.post(f"/envs/{g.json()['env_id']}/provision", headers=h_a1)
        assert prov.json()["status"] == "ready"

    def test_repeat_join_returns_same_position(self, client, h_a1, h_a2):
        client.post("/envs", json={"idempotency_key": "p-1"}, headers=h_a1)
        client.post("/envs", json={"idempotency_key": "p-2"}, headers=h_a2)
        r1 = _join(client, h_a1, "dup-w")
        r2 = _join(client, h_a1, "dup-w")
        assert r1.json()["wait"]["id"] == r2.json()["wait"]["id"]

    def test_cancel_flow_and_not_waiting_conflict(self, client, h_a1, h_a2):
        client.post("/envs", json={"idempotency_key": "q-1"}, headers=h_a1)
        client.post("/envs", json={"idempotency_key": "q-2"}, headers=h_a2)
        wid = _join(client, h_a1, "cancel-w").json()["wait"]["id"]
        r = client.post(f"/envs/waits/{wid}/cancel", headers=h_a1)
        assert r.json()["status"] == "cancelled"
        r2 = client.post(f"/envs/waits/{wid}/cancel", headers=h_a1)
        assert r2.status_code == 409
        assert r2.json()["detail"]["reason"] == "waitlist_not_waiting"

    def test_already_requested_conflict(self, client, h_a1):
        client.post("/envs", json={"idempotency_key": "exists"}, headers=h_a1)
        r = _join(client, h_a1, "exists")
        assert r.status_code == 409
        assert r.json()["detail"]["reason"] == "already_requested"

    def test_isolation_outsider_forbidden_and_404_distinct(self, client, h_a1, h_a2, h_other):
        client.post("/envs", json={"idempotency_key": "i-1"}, headers=h_a1)
        client.post("/envs", json={"idempotency_key": "i-2"}, headers=h_a2)
        wid = _join(client, h_a1, "iso-w").json()["wait"]["id"]
        # 外团队：403（即使记录存在）
        r = client.get(f"/envs/waits/{wid}", headers=h_other)
        assert r.status_code == 403
        assert r.json()["detail"]["reason"] == "forbidden"
        # 同团队但非本人同样禁止
        r = client.post(f"/envs/waits/{wid}/cancel", headers=h_a2)
        assert r.status_code == 403
        # 不存在：404
        r = client.get("/envs/waits/nope", headers=h_other)
        assert r.status_code == 404
        assert r.json()["detail"]["reason"] == "not_found"

    def test_admin_can_override(self, client, h_a1, h_a2, h_admin):
        client.post("/envs", json={"idempotency_key": "m-1"}, headers=h_a1)
        client.post("/envs", json={"idempotency_key": "m-2"}, headers=h_a2)
        wid = _join(client, h_a1, "admin-w").json()["wait"]["id"]
        assert client.get(f"/envs/waits/{wid}", headers=h_admin).status_code == 200
        r = client.post(f"/envs/waits/{wid}/cancel", headers=h_admin)
        assert r.json()["status"] == "cancelled"

    def test_wait_audit_endpoint(self, client, h_a1, h_a2):
        client.post("/envs", json={"idempotency_key": "a-1"}, headers=h_a1)
        client.post("/envs", json={"idempotency_key": "a-2"}, headers=h_a2)
        wid = _join(client, h_a1, "audit-w").json()["wait"]["id"]
        client.post(f"/envs/waits/{wid}/cancel", headers=h_a1)
        r = client.get(f"/envs/waits/{wid}/audit", headers=h_a1)
        actions = [rec["action"] for rec in r.json()]
        assert actions == ["join", "cancel"]

    def test_wait_sweep_endpoint_expires_entry(self, client, h_a1, h_a2):
        client.post("/envs", json={"idempotency_key": "s-1"}, headers=h_a1)
        client.post("/envs", json={"idempotency_key": "s-2"}, headers=h_a2)
        wid = _join(client, h_a1, "sweep-w").json()["wait"]["id"]
        entry = global_service.store.get_wait_entry(wid)
        entry.expires_at = 0
        r = client.post("/envs/waits/sweep", headers=h_a1)
        assert any(e["id"] == wid and e["status"] == "expired" for e in r.json())

    def test_list_waits_scoped(self, client, h_a1, h_a2, h_other, h_admin):
        client.post("/envs", json={"idempotency_key": "l-1"}, headers=h_a1)
        client.post("/envs", json={"idempotency_key": "l-2"}, headers=h_a2)
        _join(client, h_a1, "lw-1")
        _join(client, h_a2, "lw-2")
        _join(client, h_other, "lw-3")  # 另一个团队，有空闲额度，立即放行
        mine = client.get("/envs/waits", headers=h_a1).json()
        assert {e["owner_id"] for e in mine} == {"http-a1"}
        allw = client.get("/envs/waits", headers=h_admin).json()
        assert {"http-a1", "http-a2", "http-other"} <= {e["owner_id"] for e in allw}
