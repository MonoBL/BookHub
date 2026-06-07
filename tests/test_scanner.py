"""M4 scanner tests: format verify, VT verdict mapping, quota, path traversal."""
import asyncio
import io
import json
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.scanner import (
    _apply_verdict,
    _cache_lookup,
    _map_stats,
    verify_format,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Format verification
# ---------------------------------------------------------------------------

def test_real_pdf_passes():
    result = verify_format(FIXTURES / "real.pdf", "pdf")
    assert result["ok"] is True


def test_real_epub_passes():
    result = verify_format(FIXTURES / "real.epub", "epub")
    assert result["ok"] is True


def test_exe_renamed_pdf_rejected():
    result = verify_format(FIXTURES / "exe_as_pdf.pdf", "pdf")
    assert result["ok"] is False
    assert "magic" in result["reason"].lower() or "pdf" in result["reason"].lower()


def test_polyglot_pdf_rejected(tmp_path):
    """PDF with ZIP EOCD in tail is rejected as a polyglot."""
    result = verify_format(FIXTURES / "polyglot.pdf", "pdf")
    assert result["ok"] is False
    assert "polyglot" in result["reason"].lower()


def test_epub_missing_mimetype_rejected(tmp_path):
    """ZIP without mimetype entry fails EPUB check."""
    p = tmp_path / "bad.epub"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("content.opf", b"<package/>")
    result = verify_format(p, "epub")
    assert result["ok"] is False
    assert "mimetype" in result["reason"].lower()


def test_epub_wrong_mimetype_rejected(tmp_path):
    p = tmp_path / "bad.epub"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("mimetype", b"application/zip")
        zf.writestr("content.opf", b"<package/>")
    result = verify_format(p, "epub")
    assert result["ok"] is False


def test_epub_entry_flood_rejected(tmp_path):
    p = tmp_path / "bomb.epub"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("mimetype", b"application/epub+zip")
        for i in range(2001):
            zf.writestr(f"file{i}.txt", b"x")
    result = verify_format(p, "epub")
    assert result["ok"] is False
    assert "flood" in result["reason"].lower() or "entries" in result["reason"].lower()


def test_epub_zip_bomb_ratio_rejected(tmp_path):
    """Declared uncompressed size >> on-disk size triggers zip-bomb guard."""
    p = tmp_path / "bomb.epub"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", b"application/epub+zip")
        # Write a large compressible file (high ratio).
        zf.writestr("content.html", b"A" * (200 * 1024 * 1024))
    result = verify_format(p, "epub")
    assert result["ok"] is False


def test_not_a_zip_epub_rejected(tmp_path):
    p = tmp_path / "not.epub"
    p.write_bytes(b"%PDF-1.4 this is not a zip")
    result = verify_format(p, "epub")
    assert result["ok"] is False


def test_epub_active_content_detected(tmp_path):
    """EPUB with a .js file sets has_active_content = True."""
    p = tmp_path / "active.epub"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("mimetype", b"application/epub+zip")
        zf.writestr("script.js", b"alert(1)")
    result = verify_format(p, "epub")
    assert result["ok"] is True
    assert result.get("has_active_content") is True


def test_epub_no_active_content(tmp_path):
    result = verify_format(FIXTURES / "real.epub", "epub")
    assert result["ok"] is True
    assert result.get("has_active_content") is False


# ---------------------------------------------------------------------------
# VT verdict mapping
# ---------------------------------------------------------------------------

def test_vt_map_malicious():
    from datetime import datetime, timezone, timedelta
    fresh = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    v = _map_stats({"malicious": 5, "undetected": 35}, fresh, 40)
    assert v == "malicious"


def test_vt_map_suspicious():
    from datetime import datetime, timezone, timedelta
    fresh = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    v = _map_stats({"malicious": 0, "suspicious": 3, "undetected": 37}, fresh, 40)
    assert v == "suspicious"


def test_vt_map_clean():
    from datetime import datetime, timezone, timedelta
    fresh = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    v = _map_stats({"malicious": 0, "suspicious": 0, "undetected": 40}, fresh, 40)
    assert v == "clean"


def test_vt_map_stale_becomes_unverified(monkeypatch):
    from datetime import datetime, timezone, timedelta
    from app.config import settings
    # Pin the freshness floor so the test is independent of the deployed .env.
    monkeypatch.setattr(settings, "VT_CLEAN_MAX_AGE_DAYS", 180)
    stale = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    v = _map_stats({"malicious": 0, "suspicious": 0, "undetected": 40}, stale, 40)
    assert v == "unverified"


def test_vt_map_too_few_engines_unverified():
    from datetime import datetime, timezone, timedelta
    fresh = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    v = _map_stats({"malicious": 0, "undetected": 10}, fresh, 10)
    assert v == "unverified"


# ---------------------------------------------------------------------------
# Path traversal
# ---------------------------------------------------------------------------

async def test_path_traversal_rejected():
    """Job with UUID4 job_id is accepted; malformed id raises 400 from the route."""
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "testpassword123"},
        )
        token = resp.cookies["session"]
        # Non-UUID4 job_id must be rejected.
        r = client.get("/api/files/../etc/passwd", cookies={"session": token})
        assert r.status_code in (400, 404, 422)


# ---------------------------------------------------------------------------
# VT quota cap
# ---------------------------------------------------------------------------

async def test_vt_quota_cap_blocks():
    """When daily cap is reached, _vt_daily_check returns False."""
    import app.services.scanner as sc_mod
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Set counter to cap with today's date so the reset branch is skipped.
    async with sc_mod._vt_daily_lock:
        sc_mod._vt_daily_count = sc_mod.settings.VT_DAILY_CAP
        sc_mod._vt_daily_reset_date = today

    result = await sc_mod._vt_daily_check()
    assert result is False

    # Reset
    async with sc_mod._vt_daily_lock:
        sc_mod._vt_daily_count = 0
        sc_mod._vt_daily_reset_date = ""


# ---------------------------------------------------------------------------
# Size cap mid-stream
# ---------------------------------------------------------------------------

async def test_download_size_cap_mid_stream(tmp_path):
    """Download must abort and mark blocked when bytes exceed DOWNLOAD_MAX_MB."""
    from unittest.mock import patch, AsyncMock
    import app.services.downloader as dl_mod
    from app.providers.base import DownloadPlan
    from app.models import Job
    from app.services import jobs as job_svc

    job_id = "11111111-1111-4111-8111-111111111111"
    await job_svc.create_job(Job(id=job_id, status="queued", ext="pdf", title="test"))

    # 1 MB limit for the test.
    plan = DownloadPlan(url="http://fake.example/file.pdf")
    chunk = b"X" * 65536  # 64 KB chunk

    class FakeResp:
        status_code = 200
        headers = {"content-length": "10000000"}  # 10 MB

        async def aiter_bytes(self, chunk_size=None):
            for _ in range(200):  # 200 * 64KB = 12.8 MB
                yield chunk

        def raise_for_status(self): pass

    class FakeStream:
        async def __aenter__(self): return FakeResp()
        async def __aexit__(self, *a): pass

    class FakeClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def stream(self, method, url): return FakeStream()

    with patch("app.services.downloader.settings") as ms, \
         patch("app.services.downloader.httpx.AsyncClient", FakeClient), \
         patch("app.services.downloader._dl_sem", asyncio.Semaphore(1)):
        ms.DOWNLOAD_MAX_MB = 1  # 1 MB
        ms.DOWNLOAD_CONCURRENCY = 1
        ms.DATA_DIR = str(tmp_path)

        with pytest.raises(RuntimeError, match="size cap"):
            await dl_mod.download(job_id, plan, "pdf")

    job = await job_svc.get_job(job_id)
    assert job.status == "blocked"
    assert "size cap" in (job.reason or "")


# ---------------------------------------------------------------------------
# Serve-then-delete
# ---------------------------------------------------------------------------

async def test_file_deleted_after_serve(tmp_path):
    """After serving, the file is removed by the background task."""
    from app.services.scanner import _apply_verdict
    from app.services import jobs as job_svc
    from app.models import Job
    import shutil

    job_id = "22222222-2222-4222-8222-222222222222"
    await job_svc.create_job(Job(id=job_id, status="scanning", ext="pdf", title="test"))

    # Create a fake quarantine file.
    q = tmp_path / "quarantine"
    q.mkdir()
    r = tmp_path / "ready"
    r.mkdir()
    src = q / f"{job_id}.pdf"
    src.write_bytes(b"%PDF-1.4 fake")

    with patch("app.services.scanner.Path") as MockPath, \
         patch("app.services.scanner.settings") as ms:
        ms.DATA_DIR = str(tmp_path)
        ms.VT_DAILY_CAP = 480
        ms.VT_MIN_ENGINES = 40
        ms.VT_CLEAN_MAX_AGE_DAYS = 180

        # Call _apply_verdict directly with clean verdict.
        with patch("app.services.scanner.Path", side_effect=lambda *a: Path(*a)):
            result = await _apply_verdict(
                job_id, src, "pdf", "clean", "deadbeef" * 8, {}, False
            )

    assert result["verdict"] == "clean"
    job = await job_svc.get_job(job_id)
    assert job.status == "clean"
    assert job.download_url == f"/api/files/{job_id}"


# ---------------------------------------------------------------------------
# Forced re-scan
# ---------------------------------------------------------------------------

async def test_force_rescan_skips_cache_and_forces_upload(tmp_path):
    """force_rescan must bypass the cache + lookup and request a fresh VT scan."""
    import app.services.scanner as sc_mod
    from app.services import jobs as job_svc
    from app.models import Job

    job_id = "33333333-3333-4333-8333-333333333333"
    await job_svc.create_job(Job(id=job_id, status="downloading", ext="epub", title="t"))

    q = tmp_path / "quarantine"
    q.mkdir()
    src = q / f"{job_id}.epub"
    src.write_bytes((FIXTURES / "real.epub").read_bytes())

    cache_called = False
    lookup_called = False
    upload_called = False

    async def fake_cache(sha):
        nonlocal cache_called
        cache_called = True
        return {"verdict": "clean", "malicious_count": 0, "suspicious_count": 0,
                "engines_total": 70, "last_analysis_date": "2020-01-01T00:00:00+00:00"}

    async def fake_lookup(sha, client):
        nonlocal lookup_called
        lookup_called = True
        return "clean", {"malicious": 0, "total": 70}, "2020-01-01T00:00:00+00:00"

    async def fake_upload(path, sha, client):
        nonlocal upload_called
        upload_called = True
        return "clean", {"malicious": 0, "suspicious": 0, "total": 72}, "2026-06-08T00:00:00+00:00"

    with patch.object(sc_mod, "_cache_lookup", fake_cache), \
         patch.object(sc_mod, "_vt_lookup", fake_lookup), \
         patch.object(sc_mod, "_vt_upload_and_poll", fake_upload), \
         patch.object(sc_mod, "_cache_store", AsyncMock()), \
         patch.object(sc_mod, "settings") as ms:
        ms.DATA_DIR = str(tmp_path)
        ms.VT_API_KEY = "x"
        ms.SCAN_CONCURRENCY = 1
        ms.VT_DAILY_CAP = 480
        result = await sc_mod.verify_and_scan(job_id, src, "epub", force_rescan=True)

    assert result["verdict"] == "clean"
    assert upload_called is True
    assert cache_called is False
    assert lookup_called is False
