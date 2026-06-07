"""Security tests: IDOR on jobs/files, double-serve prevention."""
import uuid
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import Job
from app.services import jobs as job_svc

ADMIN_PW = "testpassword123"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_token(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
    return r.cookies["session"]


# --- Hardening: docs hidden, headers present, no user_id leak ---

def test_openapi_and_docs_disabled(client):
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404


def test_security_headers_present(client):
    r = client.get("/login")
    assert "content-security-policy" in r.headers
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "referrer-policy" in r.headers


def test_job_json_has_no_user_id(client, admin_token):
    job_id = str(uuid.uuid4())
    import asyncio
    asyncio.get_event_loop().run_until_complete(
        job_svc.create_job(Job(id=job_id, status="queued", ext="epub", title="T", user_id=1))
    )
    r = client.get(f"/api/jobs/{job_id}", cookies={"session": admin_token})
    assert r.status_code == 200
    assert "user_id" not in r.json()


@pytest.fixture(scope="module")
def user_b_token(client, admin_token):
    r = client.post(
        "/api/admin/users",
        json={"username": "sec_user_b", "password": "userbpassword1", "is_admin": False,
              "must_change_password": False},
        cookies={"session": admin_token},
    )
    assert r.status_code == 200
    r2 = client.post("/api/auth/login", json={"username": "sec_user_b", "password": "userbpassword1"})
    return r2.cookies["session"]


def _cookies(t):
    return {"session": t}


# ---------------------------------------------------------------------------
# IDOR: GET /api/jobs/{job_id}
# ---------------------------------------------------------------------------

def test_idor_job_status_user_b_gets_404(client, admin_token, user_b_token):
    result = {
        "id": "libgen:idor1234abcd1234abcd1234abcd1234",
        "title": "IDOR Test Book",
        "ext": "epub",
        "source": "libgen",
        "extra": {"md5": "idor1234abcd1234abcd1234abcd1234", "base": "https://libgen.la"},
    }
    r = client.post("/api/download", json={"result": result}, cookies=_cookies(admin_token))
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    # User B cannot see admin's job
    r2 = client.get(f"/api/jobs/{job_id}", cookies=_cookies(user_b_token))
    assert r2.status_code == 404

    # Admin can still see it
    r3 = client.get(f"/api/jobs/{job_id}", cookies=_cookies(admin_token))
    assert r3.status_code == 200


# ---------------------------------------------------------------------------
# IDOR: GET /api/files/{job_id}
# ---------------------------------------------------------------------------

def test_idor_file_serve_user_b_gets_404(client, admin_token, user_b_token):
    job_id = str(uuid.uuid4())
    # admin user_id=1 (first user in fresh test DB)
    job_svc._jobs[job_id] = Job(
        id=job_id, status="clean", ext="epub", title="IDOR File Test", user_id=1
    )

    ready_dir = Path(settings.DATA_DIR) / "ready"
    ready_dir.mkdir(parents=True, exist_ok=True)
    (ready_dir / f"{job_id}.epub").write_bytes(b"FAKEEPUB")

    r = client.get(f"/api/files/{job_id}", cookies=_cookies(user_b_token))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Double-serve prevention
# ---------------------------------------------------------------------------

def test_double_serve_second_request_gets_404(client, admin_token):
    job_id = str(uuid.uuid4())
    job_svc._jobs[job_id] = Job(
        id=job_id, status="clean", ext="epub", title="DoubleServe Test", user_id=1
    )

    ready_dir = Path(settings.DATA_DIR) / "ready"
    ready_dir.mkdir(parents=True, exist_ok=True)
    (ready_dir / f"{job_id}.epub").write_bytes(b"FAKEEPUB")

    # First serve: should succeed
    r1 = client.get(f"/api/files/{job_id}", cookies=_cookies(admin_token))
    assert r1.status_code == 200

    # Second serve: status is now "consumed" -> 404
    r2 = client.get(f"/api/files/{job_id}", cookies=_cookies(admin_token))
    assert r2.status_code == 404


# ---------------------------------------------------------------------------
# Cover proxy SSRF guard
# ---------------------------------------------------------------------------

def test_cover_proxy_rejects_unknown_host(client, admin_token):
    r = client.get(
        "/api/cover", params={"u": "https://evil.example.com/x.jpg"},
        cookies=_cookies(admin_token),
    )
    assert r.status_code == 400


def test_cover_proxy_rejects_non_https(client, admin_token):
    r = client.get(
        "/api/cover", params={"u": "http://libgen.la/covers/x.jpg"},
        cookies=_cookies(admin_token),
    )
    assert r.status_code == 400
