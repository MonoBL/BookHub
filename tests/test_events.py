"""M8 tests: events table, admin events endpoint, providers health, VK token override."""
import pytest
from starlette.testclient import TestClient

from app.main import app

ADMIN_PW = "testpassword123"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_token(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
    return r.cookies["session"]


@pytest.fixture
def anon():
    with TestClient(app) as c:
        yield c


def _cookies(t):
    return {"session": t}


# ---------------------------------------------------------------------------
# Admin events endpoint
# ---------------------------------------------------------------------------

def test_events_requires_admin(anon):
    r = anon.get("/api/admin/events")
    assert r.status_code == 401


def test_events_returns_list(client, admin_token):
    r = client.get("/api/admin/events", cookies=_cookies(admin_token))
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_events_limit_param(client, admin_token):
    r = client.get("/api/admin/events?limit=5", cookies=_cookies(admin_token))
    assert r.status_code == 200
    data = r.json()
    assert len(data) <= 5


def test_events_kind_filter(client, admin_token):
    r = client.get("/api/admin/events?kind=block", cookies=_cookies(admin_token))
    assert r.status_code == 200
    data = r.json()
    assert all(e["kind"] == "block" for e in data)


# ---------------------------------------------------------------------------
# Provider health endpoint
# ---------------------------------------------------------------------------

def test_providers_requires_admin(anon):
    r = anon.get("/api/admin/providers")
    assert r.status_code == 401


def test_providers_returns_all_providers(client, admin_token):
    r = client.get("/api/admin/providers", cookies=_cookies(admin_token))
    assert r.status_code == 200
    data = r.json()
    names = {p["name"] for p in data}
    assert "libgen" in names
    assert "vk" in names
    assert "annas" in names


def test_providers_have_required_fields(client, admin_token):
    r = client.get("/api/admin/providers", cookies=_cookies(admin_token))
    for p in r.json():
        assert "name" in p
        assert "enabled" in p


# ---------------------------------------------------------------------------
# VK token override
# ---------------------------------------------------------------------------

def test_vk_token_requires_admin(anon):
    r = anon.post("/api/admin/vk-token", json={"token": "tok"})
    assert r.status_code == 401


def test_vk_token_set_enables_provider(client, admin_token):
    import app.providers.vk as _vk_mod
    _vk_mod._vk_disabled = True  # simulate expired token state

    r = client.post(
        "/api/admin/vk-token",
        json={"token": "newtoken123"},
        cookies=_cookies(admin_token),
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert _vk_mod._vk_disabled is False


def test_vk_token_empty_rejected(client, admin_token):
    r = client.post(
        "/api/admin/vk-token",
        json={"token": ""},
        cookies=_cookies(admin_token),
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Events: log_event function doesn't raise
# ---------------------------------------------------------------------------

def test_log_event_does_not_raise():
    """log_event is safe to call even when queue is None (pre-lifespan)."""
    from app import events as ev_mod
    import app.events as _ev

    orig_queue = _ev._queue
    try:
        _ev._queue = None  # simulate pre-lifespan state
        _ev.log_event("block", title="test", source="libgen", sha256="aabbcc")
    finally:
        _ev._queue = orig_queue
