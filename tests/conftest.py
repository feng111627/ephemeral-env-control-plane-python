import pytest

from app.auth import Identity
from app.service import EnvService
from app.store import Store


@pytest.fixture
def store() -> Store:
    return Store(default_user_quota=3, default_team_quota=8)


@pytest.fixture
def service(store) -> EnvService:
    return EnvService(store, ttl_seconds=3600.0, max_renewals=3)


@pytest.fixture
def alice() -> Identity:
    return Identity(user_id="alice", team_id="team-a")


@pytest.fixture
def bob() -> Identity:
    return Identity(user_id="bob", team_id="team-a")


@pytest.fixture
def carol() -> Identity:
    return Identity(user_id="carol", team_id="team-b")


@pytest.fixture
def admin() -> Identity:
    return Identity(user_id="root", team_id="", is_admin=True)
