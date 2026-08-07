"""Pipeline tests: oversized archive.org files become an external direct link,
and a dead source provider falls back to another provider."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.routers import api_jobs
from app.models import Job
from app.providers.base import DownloadPlan, SearchResult


def _archive_result():
    return SearchResult(
        id="archive:item1:epub", title="Big Comic", ext="epub", source="archive",
        extra={"identifier": "item1"},
    )


def _libgen_result(title="Thinking, Fast and Slow"):
    return SearchResult(
        id="libgen:abc", title=title, ext="epub", source="libgen", extra={"md5": "abc"},
    )


def _fake_archive_provider(hits, plans):
    """A stand-in archive provider exposing the real Provider surface."""
    provider = MagicMock()
    provider.name = "archive"
    provider.enabled = True
    provider.search = AsyncMock(return_value=hits)
    provider.resolve_candidates = AsyncMock(return_value=plans)
    return provider


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


# ---------------------------------------------------------------------------
# Cross-provider fallback
# ---------------------------------------------------------------------------

async def test_dead_libgen_falls_back_to_archive():
    """Every libgen candidate failing sends the job to archive.org instead."""
    result = _libgen_result()
    libgen = AsyncMock()
    libgen.resolve_candidates = AsyncMock(return_value=[DownloadPlan(url="https://cdn5/get.php")])

    hit = SearchResult(
        id="archive:item9:epub", title="Thinking Fast and Slow", ext="epub",
        source="archive", extra={"identifier": "item9"},
    )
    archive = _fake_archive_provider(
        [hit], [DownloadPlan(url="https://archive.org/download/item9/b.epub", size_bytes=1024)],
    )

    updates = []

    async def fake_update(job_id, **kw):
        updates.append(kw)

    async def fake_download(job_id, plans, ext):
        if plans[0].url.startswith("https://cdn5"):
            raise RuntimeError("Server error '503 Service Unavailable'")
        return "/tmp/x.epub"

    with patch.object(api_jobs, "PROVIDERS", [archive]), \
         patch.object(api_jobs.job_svc, "update_job", fake_update), \
         patch.object(api_jobs.job_svc, "get_job", AsyncMock(return_value=Job(id="job1", status="error"))), \
         patch.object(api_jobs, "download_with_fallback", fake_download), \
         patch.object(api_jobs, "verify_and_scan", AsyncMock()) as scan:
        await api_jobs._run_pipeline("job1", result, libgen, "epub")

    assert scan.call_count == 1  # the fallback file reached the scanner
    assert {"source": "archive"} in updates


async def test_fallback_rejects_a_different_book():
    """A loosely-matching title is not accepted as a substitute."""
    result = _libgen_result()
    libgen = AsyncMock()
    libgen.resolve_candidates = AsyncMock(return_value=[DownloadPlan(url="https://cdn5/get.php")])

    wrong = SearchResult(
        id="archive:item9:epub", title="Cooking With Cast Iron", ext="epub",
        source="archive", extra={"identifier": "item9"},
    )
    archive = _fake_archive_provider([wrong], [DownloadPlan(url="https://archive.org/x.epub")])

    updates = []

    async def fake_update(job_id, **kw):
        updates.append(kw)

    with patch.object(api_jobs, "PROVIDERS", [archive]), \
         patch.object(api_jobs.job_svc, "update_job", fake_update), \
         patch.object(api_jobs.job_svc, "get_job", AsyncMock(return_value=Job(id="job1", status="error"))), \
         patch.object(api_jobs, "download_with_fallback",
                      AsyncMock(side_effect=RuntimeError("ReadTimeout"))), \
         patch.object(api_jobs, "verify_and_scan", AsyncMock()) as scan:
        await api_jobs._run_pipeline("job1", result, libgen, "epub")

    assert archive.resolve_candidates.call_count == 0
    assert scan.call_count == 0
    assert updates[-1]["status"] == "error"


async def test_size_cap_block_tries_a_smaller_edition():
    """An over-cap file looks for a smaller edition of the same title elsewhere."""
    result = _libgen_result()
    libgen = AsyncMock()
    libgen.resolve_candidates = AsyncMock(return_value=[DownloadPlan(url="https://cdn5/get.php")])

    hit = SearchResult(
        id="archive:item9:epub", title="Thinking, Fast and Slow", ext="epub",
        source="archive", extra={"identifier": "item9"},
    )
    # Reflowable edition, well under the cap.
    archive = _fake_archive_provider([hit], [
        DownloadPlan(url="https://archive.org/download/item9/small.epub", size_bytes=793_652),
    ])

    async def fake_download(job_id, plans, ext):
        if plans[0].url.startswith("https://cdn5"):
            raise RuntimeError("exceeds size cap (content-length)")
        return "/tmp/x.epub"

    with patch.object(api_jobs.settings, "DOWNLOAD_MAX_MB", 32), \
         patch.object(api_jobs, "PROVIDERS", [archive]), \
         patch.object(api_jobs.job_svc, "update_job", AsyncMock()), \
         patch.object(api_jobs.job_svc, "get_job",
                      AsyncMock(return_value=Job(id="job1", status="blocked", reason="exceeds size cap"))), \
         patch.object(api_jobs, "download_with_fallback", fake_download), \
         patch.object(api_jobs, "verify_and_scan", AsyncMock()) as scan:
        await api_jobs._run_pipeline("job1", result, libgen, "epub")

    assert archive.search.call_count == 1
    assert scan.call_count == 1


async def test_failed_fallback_restores_the_block_reason():
    """If the substitute also fails, the user still sees the original verdict."""
    result = _libgen_result()
    libgen = AsyncMock()
    libgen.resolve_candidates = AsyncMock(return_value=[DownloadPlan(url="https://cdn5/get.php")])

    hit = SearchResult(
        id="archive:item9:epub", title="Thinking, Fast and Slow", ext="epub",
        source="archive", extra={"identifier": "item9"},
    )
    archive = _fake_archive_provider([hit], [
        DownloadPlan(url="https://archive.org/download/item9/small.epub", size_bytes=793_652),
    ])

    updates = []

    async def fake_update(job_id, **kw):
        updates.append(kw)

    with patch.object(api_jobs.settings, "DOWNLOAD_MAX_MB", 32), \
         patch.object(api_jobs, "PROVIDERS", [archive]), \
         patch.object(api_jobs.job_svc, "update_job", fake_update), \
         patch.object(api_jobs.job_svc, "get_job",
                      AsyncMock(return_value=Job(id="job1", status="blocked", reason="exceeds size cap"))), \
         patch.object(api_jobs, "download_with_fallback",
                      AsyncMock(side_effect=RuntimeError("boom"))), \
         patch.object(api_jobs, "verify_and_scan", AsyncMock()) as scan:
        await api_jobs._run_pipeline("job1", result, libgen, "epub")

    assert scan.call_count == 0
    restored = updates[-1]
    assert restored["status"] == "blocked"
    assert restored["reason"] == "exceeds size cap"
    assert restored["source"] == "libgen"


async def test_oversized_fallback_yields_external_link():
    """A too-large archive.org substitute is handed over as a direct link."""
    result = _libgen_result()
    libgen = AsyncMock()
    libgen.resolve_candidates = AsyncMock(return_value=[DownloadPlan(url="https://cdn5/get.php")])

    hit = SearchResult(
        id="archive:item9:epub", title="Thinking, Fast and Slow", ext="epub",
        source="archive", extra={"identifier": "item9"},
    )
    archive = _fake_archive_provider([hit], [
        DownloadPlan(url="https://archive.org/download/item9/big.epub", size_bytes=200 * 1024 * 1024),
    ])

    updates = []

    async def fake_update(job_id, **kw):
        updates.append(kw)

    with patch.object(api_jobs.settings, "DOWNLOAD_MAX_MB", 32), \
         patch.object(api_jobs, "PROVIDERS", [archive]), \
         patch.object(api_jobs.job_svc, "update_job", fake_update), \
         patch.object(api_jobs.job_svc, "get_job", AsyncMock(return_value=Job(id="job1", status="error"))), \
         patch.object(api_jobs, "download_with_fallback",
                      AsyncMock(side_effect=RuntimeError("ReadTimeout"))), \
         patch.object(api_jobs, "verify_and_scan", AsyncMock()) as scan:
        await api_jobs._run_pipeline("job1", result, libgen, "epub")

    assert scan.call_count == 0
    assert updates[-1]["status"] == "external"
    assert updates[-1]["download_url"] == "https://archive.org/download/item9/big.epub"


@pytest.mark.parametrize("detail,fragment", [
    ("ReadTimeout", "did not respond"),
    ("Server error '503 Service Unavailable'", "mirrors are down"),
    ("no download candidates", "No working download link"),
    ("something odd", "Download failed"),
])
def test_client_reason_is_sanitised(detail, fragment):
    """Failure reasons explain the cause without leaking hosts, paths or keys."""
    reason = api_jobs._client_reason(RuntimeError(detail))
    assert fragment in reason
    assert "cdn" not in reason.lower()
    assert "http" not in reason.lower()
