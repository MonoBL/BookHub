"""M1 skeleton tests: health endpoint, static pages, DB init."""
import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_login_page_served(client):
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_root_page_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_convert_page_served(client):
    resp = client.get("/convert")
    assert resp.status_code == 200


def test_sw_js_served_with_header(client):
    resp = client.get("/sw.js")
    assert resp.status_code == 200
    assert resp.headers.get("service-worker-allowed") == "/"


def test_manifest_served(client):
    resp = client.get("/manifest.webmanifest")
    assert resp.status_code == 200
    assert "manifest" in resp.headers.get("content-type", "")


def _get_tables(db_path: Path) -> set:
    conn = sqlite3.connect(str(db_path))
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur.fetchall()}
    conn.close()
    return tables


def test_db_all_tables_exist(client):
    db_path = Path(settings.DATA_DIR) / "app.db"
    tables = _get_tables(db_path)
    for table in ("users", "sessions", "vt_cache", "history", "events"):
        assert table in tables, f"Missing table: {table}"


def test_db_wal_mode(client):
    db_path = Path(settings.DATA_DIR) / "app.db"
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("PRAGMA journal_mode").fetchone()
    conn.close()
    assert row[0] == "wal"


def test_data_subdirs_created(client):
    data_dir = Path(settings.DATA_DIR)
    for subdir in ("quarantine", "ready", "jobs"):
        assert (data_dir / subdir).is_dir(), f"Missing subdir: {subdir}"
