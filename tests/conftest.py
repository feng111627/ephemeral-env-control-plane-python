from __future__ import annotations

from collections import defaultdict

import pytest

from app.models import Principal, Role
from app.service import EnvService
from app.store import Store


@pytest.fixture()
def service() -> EnvService:
    return EnvService(Store())


@pytest.fixture()
def alice() -> Principal:
    return Principal(user_id="alice", team_id="team-a")


@pytest.fixture()
def bob() -> Principal:
    """与 alice 同团队。"""
    return Principal(user_id="bob", team_id="team-a")


@pytest.fixture()
def carol() -> Principal:
    """不同团队。"""
    return Principal(user_id="carol", team_id="team-b")


@pytest.fixture()
def admin() -> Principal:
    return Principal(user_id="root", team_id="ops", role=Role.ADMIN)


# ---- 测试报告：按业务（测试类）分组汇总通过情况 ----

_BUSINESS_NAMES = {
    "TestLifecycle": "生命周期推进与非法迁移",
    "TestQuota": "配额与占用统计",
    "TestQuotaConcurrency": "并发抢配额与并发回收",
    "TestPermission": "越权访问与权限判定",
    "TestIdempotency": "幂等与重复提交",
    "TestAudit": "审计追溯",
    "TestApi": "HTTP 接口端到端",
}


_RESULTS: dict[str, list[tuple[str, str]]] = defaultdict(list)


def pytest_runtest_logreport(report):
    if report.when != "call":
        return
    parts = report.nodeid.split("::")
    group = parts[1] if len(parts) >= 3 else parts[0]
    _RESULTS[group].append((parts[-1], report.outcome))


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if not _RESULTS:
        return
    terminalreporter.write_sep("=", "业务覆盖汇总")
    for cls, cases in _RESULTS.items():
        business = _BUSINESS_NAMES.get(cls, cls)
        passed = sum(1 for _, o in cases if o == "passed")
        terminalreporter.write_line(f"[{business}] 通过 {passed}/{len(cases)}")
        for name, outcome in cases:
            mark = "PASS" if outcome == "passed" else outcome.upper()
            terminalreporter.write_line(f"    {mark}  {name}")
