"""Pipeline tests: oversized archive.org files become an external direct link."""
from unittest.mock import AsyncMock, patch

import pytest

from app.routers import api_jobs
from app.providers.base import DownloadPlan, SearchResult


def _archive_result():
    return SearchResult(
        id="archive:item1:epub", title="Big Comic", ext="epub", source="archive",
        extra={"identifier": "item1"},
    )


async def test_oversized_archive_yields_external_link():
    """A >cap archive file sets status=external with the direct URL, no download."""
    big = 200 * 1024 * 1024
    provider = AsyncMock()
    provider.resolve_candidates = AsyncMock(return_value=[
        DownloadPlan(url="https://archive.org/download/item1/comic.epub", size_bytes=big),
    ])

    updates = []

    async def fake_update(job_id, **kw):
        updates.append(kw)

    with patch.object(api_jobs.settings, "DOWNLOAD_MAX_MB", 32), \
         patch.object(api_jobs.job_svc, "update_job", fake_update), \
         patch.object(api_jobs, "download_with_fallback", AsyncMock()) as dl, \
         patch.object(api_jobs, "verify_and_scan", AsyncMock()) as scan:
        await api_jobs._run_pipeline("job1", _archive_result(), provider, "epub")

    assert dl.call_count == 0  # never pulled through the server
    assert scan.call_count == 0
    last = updates[-1]
    assert last["status"] == "external"
    assert last["download_url"] == "https://archive.org/download/item1/comic.epub"
    assert "MB" in last["reason"]


async def test_small_archive_downloads_normally():
    """A within-cap archive file goes through download + scan as usual."""
    small = 1 * 1024 * 1024
    provider = AsyncMock()
    provider.resolve_candidates = AsyncMock(return_value=[
        DownloadPlan(url="https://archive.org/download/item1/real.epub", size_bytes=small),
    ])

    with patch.object(api_jobs.settings, "DOWNLOAD_MAX_MB", 32), \
         patch.object(api_jobs.job_svc, "update_job", AsyncMock()), \
         patch.object(api_jobs, "download_with_fallback", AsyncMock(return_value="/tmp/x.epub")) as dl, \
         patch.object(api_jobs, "verify_and_scan", AsyncMock()) as scan:
        await api_jobs._run_pipeline("job1", _archive_result(), provider, "epub")

    assert dl.call_count == 1
    assert scan.call_count == 1


async def test_oversized_non_archive_still_downloads():
    """The external-link shortcut is archive-only; libgen keeps going through us."""
    big = 200 * 1024 * 1024
    result = SearchResult(id="libgen:x", title="T", ext="pdf", source="libgen", extra={"md5": "x"})
    provider = AsyncMock()
    provider.resolve_candidates = AsyncMock(return_value=[
        DownloadPlan(url="https://cdn/get.php", size_bytes=big),
    ])

    with patch.object(api_jobs.settings, "DOWNLOAD_MAX_MB", 32), \
         patch.object(api_jobs.job_svc, "update_job", AsyncMock()), \
         patch.object(api_jobs, "download_with_fallback", AsyncMock(return_value="/tmp/x.pdf")) as dl, \
         patch.object(api_jobs, "verify_and_scan", AsyncMock()):
        await api_jobs._run_pipeline("job1", result, provider, "pdf")

    assert dl.call_count == 1  # not short-circuited
