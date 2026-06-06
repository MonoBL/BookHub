"""M2 auth tests: sessions, argon2, admin CRUD, must-change flow, rate limit."""
import pytest
from starlette.testclient import TestClient

from app import auth as _auth
from app.main import app

ADMIN_PW = "testpassword123"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_token(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
    assert resp.status_code == 200, resp.text
    return resp.cookies["session"]


def _cookies(token: str) -> dict:
    return {"session": token}


# --- Login ---

def test_login_ok(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
    assert resp.status_code == 200
    assert "session" in resp.cookies
    assert "must_change_password" in resp.json()


def test_login_wrong_password(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "wrongpassword"})
    assert resp.status_code == 401


def test_login_unknown_user(client):
    resp = client.post("/api/auth/login", json={"username": "nobody", "password": "whatever"})
    assert resp.status_code == 401


def test_login_sets_httponly_cookie(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
    raw = resp.headers.get("set-cookie", "")
    assert "httponly" in raw.lower()


def test_logout_clears_session(client):
    # Use a fresh login so we don't invalidate the module-level admin_token.
    resp0 = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
    temp_token = resp0.cookies["session"]

    client.post("/api/auth/logout", cookies=_cookies(temp_token))

    # The logged-out token must now be invalid.
    resp2 = client.get("/api/admin/users", cookies=_cookies(temp_token))
    assert resp2.status_code == 401


# --- Protected routes ---

def test_api_requires_session(client):
    # Run while client has no cookie (logout cleared it above).
    resp = client.get("/api/admin/users")
    assert resp.status_code == 401


def test_health_needs_no_session(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200


def test_protected_page_redirects_to_login(client):
    # TestClient follows redirects by default; disable to check the actual status code.
    resp = client.get("/admin", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers.get("location", "")


# --- Admin guard ---

# admin_token fixture first used here; it logs in and stores admin cookie on client.

def test_admin_can_list_users(client, admin_token):
    resp = client.get("/api/admin/users", cookies=_cookies(admin_token))
    assert resp.status_code == 200
    users = resp.json()
    assert any(u["username"] == "admin" for u in users)


def test_non_admin_blocked_from_admin_routes(client, admin_token):
    resp = client.post(
        "/api/admin/users",
        cookies=_cookies(admin_token),
        json={"username": "plain_user", "password": "plainpass123", "is_admin": False},
    )
    assert resp.status_code == 200

    resp2 = client.post(
        "/api/auth/login", json={"username": "plain_user", "password": "plainpass123"}
    )
    assert resp2.status_code == 200
    user_token = resp2.cookies["session"]

    resp3 = client.get("/api/admin/users", cookies=_cookies(user_token))
    assert resp3.status_code == 403


# --- Admin user CRUD ---

def test_create_user_duplicate_rejected(client, admin_token):
    resp = client.post(
        "/api/admin/users",
        cookies=_cookies(admin_token),
        json={"username": "admin", "password": "somepassword123", "is_admin": False},
    )
    assert resp.status_code == 409


def test_admin_cannot_delete_self(client, admin_token):
    users = client.get("/api/admin/users", cookies=_cookies(admin_token)).json()
    admin_id = next(u["id"] for u in users if u["username"] == "admin")

    resp = client.delete(f"/api/admin/users/{admin_id}", cookies=_cookies(admin_token))
    assert resp.status_code == 400


def test_create_and_delete_user(client, admin_token):
    resp = client.post(
        "/api/admin/users",
        cookies=_cookies(admin_token),
        json={"username": "temp_del_user", "password": "temppassword1", "is_admin": False},
    )
    assert resp.status_code == 200
    user_id = resp.json()["id"]

    resp2 = client.delete(f"/api/admin/users/{user_id}", cookies=_cookies(admin_token))
    assert resp2.status_code == 200

    # Deleted user cannot log in.
    resp3 = client.post(
        "/api/auth/login", json={"username": "temp_del_user", "password": "temppassword1"}
    )
    assert resp3.status_code == 401


# --- must_change_password flow ---

def test_must_change_flow(client, admin_token):
    resp = client.post(
        "/api/admin/users",
        cookies=_cookies(admin_token),
        json={"username": "mcp_user", "password": "mcppassword1", "is_admin": False},
    )
    assert resp.status_code == 200
    user_id = resp.json()["id"]

    # reset-password sets must_change_password=1
    client.post(
        f"/api/admin/users/{user_id}/reset-password",
        cookies=_cookies(admin_token),
        json={"new_password": "tmppass1234"},
    )

    resp2 = client.post(
        "/api/auth/login", json={"username": "mcp_user", "password": "tmppass1234"}
    )
    assert resp2.status_code == 200
    assert resp2.json()["must_change_password"] is True
    mcp_token = resp2.cookies["session"]

    # Normal API routes blocked until password changed.
    resp3 = client.get("/api/admin/users", cookies=_cookies(mcp_token))
    assert resp3.status_code in (401, 403)

    # change-password itself is exempt from the block.
    resp4 = client.post(
        "/api/auth/change-password",
        cookies=_cookies(mcp_token),
        json={"current_password": "tmppass1234", "new_password": "newpassword99"},
    )
    assert resp4.status_code == 200

    # must_change_password is now cleared.
    resp5 = client.post(
        "/api/auth/login", json={"username": "mcp_user", "password": "newpassword99"}
    )
    assert resp5.status_code == 200
    assert resp5.json()["must_change_password"] is False


def test_change_password_wrong_current(client, admin_token):
    resp = client.post(
        "/api/auth/change-password",
        cookies=_cookies(admin_token),
        json={"current_password": "definitelywrong", "new_password": "newpassword99"},
    )
    assert resp.status_code == 401


# --- Rate limit ---

def test_rate_limit_trips_after_five_failures(client):
    _auth._clear_rate_limits()

    for _ in range(5):
        client.post("/api/auth/login", json={"username": "admin", "password": "wrongpw"})

    resp = client.post("/api/auth/login", json={"username": "admin", "password": "wrongpw"})
    assert resp.status_code == 429

    # Clear so subsequent login tests are not affected.
    _auth._clear_rate_limits()


# --- Page redirects for authed users ---

def test_login_page_redirects_authed_user(client):
    # Fresh login (rate limits cleared above).
    resp0 = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
    assert resp0.status_code == 200
    fresh_token = resp0.cookies["session"]

    resp = client.get("/login", follow_redirects=False, cookies=_cookies(fresh_token))
    assert resp.status_code == 302
    assert resp.headers.get("location") == "/"
