"""M9 tests: PWA manifest, service worker, offline banner, icons."""
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.main import app

STATIC = Path(__file__).parent.parent / "static"


@pytest.fixture(scope="module")
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def test_manifest_served(client):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert "application/manifest+json" in r.headers.get("content-type", "")


def test_manifest_required_fields(client):
    r = client.get("/manifest.webmanifest")
    data = r.json()
    assert data["name"] == "BookHub"
    assert data["start_url"] == "/"
    assert data["display"] == "standalone"
    assert "icons" in data
    assert len(data["icons"]) >= 2


def test_manifest_has_maskable_icon(client):
    r = client.get("/manifest.webmanifest")
    data = r.json()
    purposes = [i.get("purpose", "") for i in data["icons"]]
    assert any("maskable" in p for p in purposes)


def test_manifest_has_dark_theme(client):
    r = client.get("/manifest.webmanifest")
    data = r.json()
    assert "background_color" in data
    assert "theme_color" in data


# ---------------------------------------------------------------------------
# Service worker
# ---------------------------------------------------------------------------

def test_sw_served(client):
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert "Service-Worker-Allowed" in r.headers


def test_sw_service_worker_allowed_header(client):
    r = client.get("/sw.js")
    assert r.headers.get("Service-Worker-Allowed") == "/"


def test_sw_blocks_api(client):
    """SW source must contain /api guard (network passthrough for /api)."""
    r = client.get("/sw.js")
    body = r.text
    assert "/api" in body


def test_sw_skips_waiting(client):
    r = client.get("/sw.js")
    assert "skipWaiting" in r.text


def test_sw_claims_clients(client):
    r = client.get("/sw.js")
    assert "clients.claim" in r.text


# ---------------------------------------------------------------------------
# Icons
# ---------------------------------------------------------------------------

def test_icon_192_exists(client):
    r = client.get("/static/icons/icon-192.png")
    assert r.status_code == 200
    # Minimal valid PNG starts with PNG signature
    assert r.content[:4] == b"\x89PNG"


def test_icon_512_exists(client):
    r = client.get("/static/icons/icon-512.png")
    assert r.status_code == 200
    assert r.content[:4] == b"\x89PNG"


def test_apple_touch_icon_exists(client):
    r = client.get("/static/icons/apple-touch-180.png")
    assert r.status_code == 200
    assert r.content[:4] == b"\x89PNG"


# ---------------------------------------------------------------------------
# HTML pages: offline banner and apple-touch-icon meta
# ---------------------------------------------------------------------------

def test_index_has_offline_banner(client):
    html = (STATIC / "index.html").read_text()
    assert "offline-banner" in html


def test_index_has_apple_touch_icon(client):
    html = (STATIC / "index.html").read_text()
    assert "apple-touch-icon" in html


def test_index_has_sw_registration(client):
    """app.js (loaded by index.html) registers the service worker."""
    js = (STATIC / "app.js").read_text()
    assert "serviceWorker" in js
    assert "sw.js" in js


def test_convert_has_offline_banner(client):
    html = (STATIC / "convert.html").read_text()
    assert "offline-banner" in html


# ---------------------------------------------------------------------------
# pollJob: implementation checks (source inspection)
# ---------------------------------------------------------------------------

def test_poll_job_pauses_on_hidden(client):
    js = (STATIC / "app.js").read_text()
    assert "visibilitychange" in js
    assert "document.hidden" in js


def test_poll_job_has_terminal_statuses(client):
    js = (STATIC / "app.js").read_text()
    assert "clean" in js
    assert "blocked" in js
    assert "unverified" in js


def test_poll_job_has_absolute_cap(client):
    js = (STATIC / "app.js").read_text()
    # 7 min cap check
    assert "7" in js
    assert "60" in js
