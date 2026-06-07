"""M2 auth tests: sessions, argon2, admin CRUD, must-change flow, rate limit."""
import secrets
import time

import pytest
from starlette.testclient import TestClient

from app import auth as _auth
from app.config import settings as _settings
from app.db import write_db, get_db
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


# --- First-run setup ---

def test_needs_setup_false_when_admin_exists(client):
    # The bootstrap admin already exists in the test DB.
    r = client.get("/api/auth/needs-setup")
    assert r.status_code == 200
    assert r.json()["needs_setup"] is False


def test_setup_blocked_once_users_exist(client):
    r = client.post("/api/auth/setup", json={"username": "newadmin", "password": "longenough12"})
    assert r.status_code == 403


def test_setup_page_redirects_when_admin_exists(client):
    r = client.get("/setup", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


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


def test_cookie_secure_flag(client, monkeypatch):
    monkeypatch.setattr(_settings, "COOKIE_SECURE", True)
    resp = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
    assert "; secure" in resp.headers.get("set-cookie", "").lower()

    monkeypatch.setattr(_settings, "COOKIE_SECURE", False)
    resp2 = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
    assert "; secure" not in resp2.headers.get("set-cookie", "").lower()


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

def test_create_user_defaults_to_must_change_password(client, admin_token):
    resp = client.post(
        "/api/admin/users",
        cookies=_cookies(admin_token),
        json={"username": "mc_default_user", "password": "defaultpass123"},
    )
    assert resp.status_code == 200
    assert resp.json()["must_change_password"] is True

    # On login the flag is reported, and protected routes are gated until changed.
    login = client.post("/api/auth/login", json={"username": "mc_default_user", "password": "defaultpass123"})
    assert login.status_code == 200
    assert login.json()["must_change_password"] is True
    tok = login.cookies["session"]
    gated = client.get("/api/history", cookies=_cookies(tok))
    assert gated.status_code == 403  # must change password first


def test_create_user_can_opt_out_of_must_change(client, admin_token):
    resp = client.post(
        "/api/admin/users",
        cookies=_cookies(admin_token),
        json={"username": "no_mc_user", "password": "defaultpass123", "must_change_password": False},
    )
    assert resp.status_code == 200
    assert resp.json()["must_change_password"] is False


def test_create_user_duplicate_rejected(client, admin_token):
    resp = client.post(
        "/api/admin/users",
        cookies=_cookies(admin_token),
        json={"username": "admin", "password": "somepassword123", "is_admin": False},
    )
    assert resp.status_code == 409


def test_create_user_short_password_rejected(client, admin_token):
    resp = client.post(
        "/api/admin/users",
        cookies=_cookies(admin_token),
        json={"username": "shortpw_user", "password": "abc", "is_admin": False},
    )
    assert resp.status_code == 400


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


def test_delete_cascade_invalidates_sessions(client, admin_token):
    resp = client.post(
        "/api/admin/users",
        cookies=_cookies(admin_token),
        json={"username": "cascade_user", "password": "cascadepass1", "is_admin": False},
    )
    user_id = resp.json()["id"]

    resp2 = client.post(
        "/api/auth/login", json={"username": "cascade_user", "password": "cascadepass1"}
    )
    user_token = resp2.cookies["session"]

    # Delete the user - cascade should remove their sessions.
    client.delete(f"/api/admin/users/{user_id}", cookies=_cookies(admin_token))

    # Token T must now be rejected.
    resp3 = client.get("/api/admin/users", cookies=_cookies(user_token))
    assert resp3.status_code == 401


async def test_expired_session_rejected(client, admin_token):
    expired_token = "expired_" + secrets.token_hex(12)
    async with write_db() as db:
        await db.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, 1, '2020-01-01T00:00:00+00:00')",
            (expired_token,),
        )

    resp = client.get("/api/admin/users", cookies={"session": expired_token})
    assert resp.status_code == 401


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
    assert resp3.status_code == 403

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


def test_must_change_page_redirect(client, admin_token):
    resp = client.post(
        "/api/admin/users",
        cookies=_cookies(admin_token),
        json={"username": "mcp_page_user", "password": "mcppagepass1", "is_admin": False},
    )
    user_id = resp.json()["id"]

    client.post(
        f"/api/admin/users/{user_id}/reset-password",
        cookies=_cookies(admin_token),
        json={"new_password": "mcptemppass1"},
    )

    resp2 = client.post(
        "/api/auth/login", json={"username": "mcp_page_user", "password": "mcptemppass1"}
    )
    mcp_token = resp2.cookies["session"]

    # GET "/" must redirect to /change-password for must_change users.
    resp3 = client.get("/", follow_redirects=False, cookies=_cookies(mcp_token))
    assert resp3.status_code == 302
    assert resp3.headers.get("location") == "/change-password"

    # GET "/change-password" must be accessible (200).
    resp4 = client.get("/change-password", follow_redirects=False, cookies=_cookies(mcp_token))
    assert resp4.status_code == 200


def test_change_password_kills_other_sessions(client, admin_token):
    resp = client.post(
        "/api/admin/users",
        cookies=_cookies(admin_token),
        json={"username": "pwchange_user", "password": "pwchangepass1", "is_admin": False},
    )
    user_id = resp.json()["id"]

    # Login twice to get two distinct session tokens.
    resp_a = client.post(
        "/api/auth/login", json={"username": "pwchange_user", "password": "pwchangepass1"}
    )
    token_a = resp_a.cookies["session"]

    resp_b = client.post(
        "/api/auth/login", json={"username": "pwchange_user", "password": "pwchangepass1"}
    )
    token_b = resp_b.cookies["session"]

    # Change password using session A - should kill session B.
    resp_cp = client.post(
        "/api/auth/change-password",
        cookies=_cookies(token_a),
        json={"current_password": "pwchangepass1", "new_password": "newpwchange99"},
    )
    assert resp_cp.status_code == 200

    # Session B must be dead.
    resp_b_check = client.post(
        "/api/auth/change-password",
        cookies=_cookies(token_b),
        json={"current_password": "pwchangepass1", "new_password": "anotherpw1234"},
    )
    assert resp_b_check.status_code == 401

    # Session A must still be alive.
    resp_a_check = client.post(
        "/api/auth/change-password",
        cookies=_cookies(token_a),
        json={"current_password": "newpwchange99", "new_password": "finalpassword99"},
    )
    assert resp_a_check.status_code == 200


def test_change_password_wrong_current(client, admin_token):
    resp = client.post(
        "/api/auth/change-password",
        cookies=_cookies(admin_token),
        json={"current_password": "definitelywrong", "new_password": "newpassword99"},
    )
    assert resp.status_code == 401


# --- Rate limit ---

def test_rate_limit_window_expiry(client):
    _auth._clear_rate_limits()

    # Seed 5 failures that are older than the 60 s window.
    old_ts = time.monotonic() - 70
    _auth._failure_timestamps["testclient:admin"] = [old_ts] * 5

    # Old failures pruned during check_rate_limit; login must succeed.
    resp = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
    assert resp.status_code == 200

    _auth._clear_rate_limits()


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
