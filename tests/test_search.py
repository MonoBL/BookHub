"""M5 tests: search endpoint shape, job status transitions, global auth guard."""
import re
import pytest
from starlette.testclient import TestClient

from app.main import app

ADMIN_PW = "testpassword123"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def token(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
    return r.cookies["session"]


@pytest.fixture
def anon():
    """Fresh TestClient with no session cookies - for auth-guard tests."""
    with TestClient(app) as c:
        yield c


def _cookies(t):
    return {"session": t}


# ---------------------------------------------------------------------------
# Global auth guard: every /api/* route needs a cookie (except health + login)
# Uses a fresh anon client so no cookies are stored from prior tests.
# ---------------------------------------------------------------------------

def test_health_no_auth_needed(anon):
    r = anon.get("/api/health")
    assert r.status_code == 200


def test_login_endpoint_no_auth_needed(anon):
    r = anon.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
    assert r.status_code == 200


def test_global_auth_no_cookie(anon):
    """No session cookie -> 401 for all protected /api endpoints."""
    protected = [
        ("GET", "/api/search?q=test"),
        ("GET", "/api/jobs/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        ("POST", "/api/download"),
        ("GET", "/api/files/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        ("GET", "/api/history"),
        ("GET", "/api/admin/users"),
    ]
    for method, path in protected:
        r = anon.request(method, path)
        assert r.status_code == 401, f"{method} {path} -> {r.status_code}"


# ---------------------------------------------------------------------------
# Search endpoint shape
# ---------------------------------------------------------------------------

def test_search_returns_results_and_providers(client, token):
    r = client.get("/api/search?q=python", cookies=_cookies(token))
    assert r.status_code == 200
    data = r.json()
    assert "results" in data
    assert "providers" in data
    assert isinstance(data["results"], list)
    assert isinstance(data["providers"], list)


def test_search_providers_have_required_fields(client, token):
    r = client.get("/api/search?q=gatsby", cookies=_cookies(token))
    data = r.json()
    for p in data["providers"]:
        assert "name" in p
        assert "status" in p
        assert p["status"] in ("ok", "error", "timeout", "disabled")


def test_search_ext_epub_filter(client, token):
    r = client.get("/api/search?q=test&ext=epub", cookies=_cookies(token))
    assert r.status_code == 200


def test_search_ext_pdf_filter(client, token):
    r = client.get("/api/search?q=test&ext=pdf", cookies=_cookies(token))
    assert r.status_code == 200


def test_search_missing_query_rejected(client, token):
    r = client.get("/api/search", cookies=_cookies(token))
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Job status transitions
# ---------------------------------------------------------------------------

def test_jobs_invalid_uuid_rejected(client, token):
    r = client.get("/api/jobs/not-a-uuid", cookies=_cookies(token))
    assert r.status_code == 400


def test_jobs_unknown_id_returns_404(client, token):
    r = client.get(
        "/api/jobs/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        cookies=_cookies(token),
    )
    assert r.status_code == 404


def test_download_returns_job_id(client, token):
    """POST /api/download with a minimal result returns a UUID4 job_id."""
    result = {
        "id": "libgen:aabbcc1234567890aabbcc1234567890",
        "title": "Test Book",
        "author": None,
        "ext": "epub",
        "size_bytes": None,
        "source": "libgen",
        "extra": {"md5": "aabbcc1234567890aabbcc1234567890", "base": "https://libgen.la"},
    }
    r = client.post(
        "/api/download",
        json={"result": result},
        cookies=_cookies(token),
    )
    assert r.status_code == 200
    data = r.json()
    assert "job_id" in data
    uuid4_re = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        re.IGNORECASE,
    )
    assert uuid4_re.match(data["job_id"])


def test_job_status_readable_after_create(client, token):
    """After creating a job, its status is accessible via GET /api/jobs/{id}."""
    result = {
        "id": "libgen:ff00ff1234567890ff00ff1234567890",
        "title": "Test Book 2",
        "author": None,
        "ext": "pdf",
        "size_bytes": None,
        "source": "libgen",
        "extra": {"md5": "ff00ff1234567890ff00ff1234567890", "base": "https://libgen.la"},
    }
    r = client.post("/api/download", json={"result": result}, cookies=_cookies(token))
    job_id = r.json()["job_id"]

    r2 = client.get(f"/api/jobs/{job_id}", cookies=_cookies(token))
    assert r2.status_code == 200
    job = r2.json()
    assert job["id"] == job_id
    assert "status" in job
    assert job["status"] in (
        "queued", "downloading", "verifying", "scanning",
        "clean", "blocked", "unverified", "error",
    )


# ---------------------------------------------------------------------------
# Result ordering: smallest file first, unknown sizes last
# ---------------------------------------------------------------------------

async def test_search_sorts_by_size_ascending(monkeypatch):
    from app.services import search as search_mod
    from app.providers.base import SearchResult

    class FakeProvider:
        name = "fake"
        enabled = True
        async def search(self, query, ext_filter):
            return [
                SearchResult(id="fake:a", title="Big", ext="epub", source="fake", size_bytes=5_000_000),
                SearchResult(id="fake:b", title="Unknown", ext="epub", source="fake", size_bytes=None),
                SearchResult(id="fake:c", title="Small", ext="epub", source="fake", size_bytes=200_000),
            ]

    monkeypatch.setattr(search_mod, "PROVIDERS", [FakeProvider()])
    search_mod._cache.clear()

    out = await search_mod.search("anything", ["epub"])
    titles = [r["title"] for r in out["results"]]
    assert titles == ["Small", "Big", "Unknown"]
