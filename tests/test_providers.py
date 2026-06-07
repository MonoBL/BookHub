"""M3/M7 provider tests: Libgen, VK, Anna's Archive search, CF detection, filter."""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from urllib.parse import urlsplit

from app.providers.base import DownloadPlan, SearchResult
from app.providers.libgen import LibgenProvider, _parse_size

FIXTURES = Path(__file__).parent / "fixtures"


def _html(name: str) -> str:
    return (FIXTURES / name).read_text()


# --- parse fixture HTML ---

def test_libgen_parse_returns_epub_and_pdf():
    provider = LibgenProvider()
    results = provider._parse_results(_html("libgen_search.html"), "https://libgen.la", [])
    assert len(results) == 2  # doc row filtered by ext check
    exts = {r.ext for r in results}
    assert exts == {"epub", "pdf"}


def test_libgen_parse_extracts_title_author_md5():
    provider = LibgenProvider()
    results = provider._parse_results(_html("libgen_search.html"), "https://libgen.la", [])
    epub = next(r for r in results if r.ext == "epub")
    assert epub.title == "The Great Gatsby"
    assert epub.author == "F. Scott Fitzgerald"
    assert epub.extra["md5"] == "aabbcc1234567890aabbcc1234567890"


def test_libgen_parse_size_in_result():
    provider = LibgenProvider()
    results = provider._parse_results(_html("libgen_search.html"), "https://libgen.la", [])
    epub = next(r for r in results if r.ext == "epub")
    assert epub.size_bytes == 512 * 1024


def test_libgen_result_id_format():
    provider = LibgenProvider()
    results = provider._parse_results(_html("libgen_search.html"), "https://libgen.la", [])
    for r in results:
        assert r.id.startswith("libgen:")
        assert r.source == "libgen"


# --- header-based column mapping ---

def test_libgen_header_column_mapping_shuffled():
    """Parser must work regardless of column order."""
    provider = LibgenProvider()
    results = provider._parse_results(_html("libgen_search_shuffled.html"), "https://libgen.la", [])
    assert len(results) == 2
    epub = next(r for r in results if r.ext == "epub")
    assert epub.title == "The Great Gatsby"
    assert epub.author == "F. Scott Fitzgerald"
    assert epub.extra["md5"] == "aabbcc1234567890aabbcc1234567890"


def test_libgen_no_tablelibgen_returns_empty():
    provider = LibgenProvider()
    results = provider._parse_results("<html><body>no table here</body></html>", "https://libgen.la", [])
    assert results == []


# --- EPUB/PDF filter ---

def test_libgen_epub_filter_only():
    provider = LibgenProvider()
    results = provider._parse_results(_html("libgen_search.html"), "https://libgen.la", ["epub"])
    assert all(r.ext == "epub" for r in results)
    assert len(results) == 1


def test_libgen_pdf_filter_only():
    provider = LibgenProvider()
    results = provider._parse_results(_html("libgen_search.html"), "https://libgen.la", ["pdf"])
    assert all(r.ext == "pdf" for r in results)
    assert len(results) == 1


def test_libgen_no_filter_returns_both():
    provider = LibgenProvider()
    results = provider._parse_results(_html("libgen_search.html"), "https://libgen.la", [])
    assert len(results) == 2


# --- Cloudflare detection ---

def test_cloudflare_detected_by_503():
    assert LibgenProvider._is_cloudflare(503, "") is True


def test_cloudflare_detected_by_403():
    assert LibgenProvider._is_cloudflare(403, "") is True


def test_cloudflare_detected_by_body_just_a_moment():
    assert LibgenProvider._is_cloudflare(200, "Just a moment please...") is True


def test_cloudflare_detected_by_challenge_platform():
    assert LibgenProvider._is_cloudflare(200, "challenge-platform active") is True


def test_cloudflare_detected_by_cf_chl():
    assert LibgenProvider._is_cloudflare(200, "cf_chl present") is True


def test_cloudflare_not_detected_on_normal_page():
    assert LibgenProvider._is_cloudflare(200, "<html>Normal page</html>") is False


# --- ads.php / resolve parsing ---

def test_libgen_resolve_extracts_get_url():
    """ads.php HTML parse: get.php URL with key is extracted correctly."""
    url = LibgenProvider._extract_get_url(_html("libgen_ads.html"), "https://libgen.la/ads.php?md5=aabb")
    assert url is not None
    assert "get.php" in url
    assert "key=" in url
    assert "md5=" in url


def test_libgen_resolve_no_key_returns_none():
    html = "<html><body><a href='/other?md5=abc'>no key link</a></body></html>"
    url = LibgenProvider._extract_get_url(html, "https://libgen.la/ads.php?md5=abc")
    assert url is None


# --- mirror fallback ---

async def test_libgen_mirror_fallback():
    """When first mirror returns 503, the second mirror is used."""
    provider = LibgenProvider()
    provider._mirror_cache = None  # force re-probe

    call_log: list[str] = []

    class FakeResp:
        def __init__(self, status: int, text: str = "ok"):
            self.status_code = status
            self.text = text

    class FakeClient:
        def __init__(self, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def get(self, url: str, **kw):
            call_log.append(url)
            if "badmirror" in url:
                return FakeResp(503, "Down")
            return FakeResp(200, "<html>ok</html>")

    with patch("app.providers.libgen.settings") as mock_cfg, \
         patch("app.providers.libgen.httpx.AsyncClient", FakeClient):
        mock_cfg.LIBGEN_MIRRORS = "badmirror,goodmirror"
        result = await provider._find_mirror()

    assert result == "https://goodmirror"
    assert any("badmirror" in u for u in call_log)


async def test_libgen_mirror_all_down_returns_none():
    """When all mirrors fail, _find_mirror returns None."""
    provider = LibgenProvider()
    provider._mirror_cache = None

    class FakeResp:
        status_code = 503
        text = "Down"

    class FakeClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url: str, **kw): return FakeResp()

    with patch("app.providers.libgen.settings") as mock_cfg, \
         patch("app.providers.libgen.httpx.AsyncClient", FakeClient):
        mock_cfg.LIBGEN_MIRRORS = "m1,m2"
        result = await provider._find_mirror()

    assert result is None


# --- cover parsing ---

def test_libgen_parse_extracts_cover_url():
    """A row <img> becomes an absolute cover_url; data-src wins over src."""
    html = """
    <table id="tablelibgen"><tbody>
    <tr style="font-weight:bold"><td>ID</td><td>Title</td><td>Author(s)</td>
      <td>Size</td><td>Extension</td><td>Mirrors</td></tr>
    <tr>
      <td><img src="/img/blank.png" data-src="/covers/abc.jpg"></td>
      <td><a href="/book/index.php?md5=aabbcc1234567890aabbcc1234567890">A Book</a></td>
      <td>Someone</td><td>1 Mb</td><td>epub</td>
      <td><a href="/ads.php?md5=aabbcc1234567890aabbcc1234567890">GET</a></td>
    </tr>
    </tbody></table>"""
    provider = LibgenProvider()
    results = provider._parse_results(html, "https://libgen.la", [])
    assert len(results) == 1
    assert results[0].cover_url == "https://libgen.la/covers/abc.jpg"


def test_libgen_parse_skips_blank_cover():
    """Placeholder/blank images yield no cover_url."""
    html = """
    <table id="tablelibgen"><tbody>
    <tr style="font-weight:bold"><td>Title</td><td>Extension</td><td>Mirrors</td></tr>
    <tr>
      <td><a href="/book/index.php?md5=ff00ff1234567890ff00ff1234567890">B</a>
          <img src="https://libgen.la/img/blank.png"></td>
      <td>pdf</td>
      <td><a href="/ads.php?md5=ff00ff1234567890ff00ff1234567890">GET</a></td>
    </tr>
    </tbody></table>"""
    provider = LibgenProvider()
    results = provider._parse_results(html, "https://libgen.la", [])
    assert len(results) == 1
    assert results[0].cover_url is None


# --- resolve_candidates: distinct CDN hosts across mirrors ---

async def test_libgen_resolve_candidates_dedupes_by_host():
    """Mirrors handing the same CDN host collapse to one plan; distinct hosts stack."""
    provider = LibgenProvider()

    class FakeResp:
        def __init__(self, status, text):
            self.status_code = status
            self.text = text
            self.cookies = {}

    # m1 -> host A, m2 -> host A again (dup), m3 -> host B
    pages = {
        "m1": "<a href='https://cdnA/get.php?md5=x&key=K1'>g</a>",
        "m2": "<a href='https://cdnA/get.php?md5=x&key=K2'>g</a>",
        "m3": "<a href='https://cdnB/get.php?md5=x&key=K3'>g</a>",
    }

    class FakeClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url, **kw):
            for m, body in pages.items():
                if f"//{m}/" in url:
                    return FakeResp(200, body)
            return FakeResp(404, "")

    result = SearchResult(id="libgen:x", title="T", ext="epub", source="libgen", extra={"md5": "x"})
    with patch("app.providers.libgen.settings") as cfg, \
         patch("app.providers.libgen.httpx.AsyncClient", FakeClient):
        cfg.LIBGEN_MIRRORS = "m1,m2,m3"
        cfg.PROVIDER_RESOLVE_TIMEOUT_S = 30
        plans = await provider.resolve_candidates(result)

    hosts = [urlsplit(p.url).netloc for p in plans]
    assert hosts == ["cdnA", "cdnB"]


# --- download fallback across candidates ---

async def test_download_with_fallback_tries_next_on_failure():
    """First candidate 503s, second succeeds -> returns the good path."""
    from app.services import downloader

    calls = []

    async def fake_download(job_id, plan, ext):
        calls.append(plan.url)
        if plan.url.endswith("bad"):
            raise RuntimeError("Server error '503'")
        return Path(f"/tmp/{job_id}.{ext}")

    async def fake_get_job(job_id):
        return MagicMock(status="error")

    plans = [DownloadPlan(url="https://cdnA/bad"), DownloadPlan(url="https://cdnB/good")]
    with patch.object(downloader, "download", fake_download), \
         patch.object(downloader.job_svc, "get_job", fake_get_job):
        path = await downloader.download_with_fallback("job1", plans, "epub")

    assert str(path).endswith("job1.epub")
    assert calls == ["https://cdnA/bad", "https://cdnB/good"]


async def test_download_with_fallback_blocked_is_final():
    """A size-cap 'blocked' stops the loop without trying other hosts."""
    from app.services import downloader

    calls = []

    async def fake_download(job_id, plan, ext):
        calls.append(plan.url)
        raise RuntimeError("exceeds size cap")

    async def fake_get_job(job_id):
        return MagicMock(status="blocked")

    plans = [DownloadPlan(url="https://cdnA/big"), DownloadPlan(url="https://cdnB/big")]
    with patch.object(downloader, "download", fake_download), \
         patch.object(downloader.job_svc, "get_job", fake_get_job):
        with pytest.raises(RuntimeError, match="size cap"):
            await downloader.download_with_fallback("job1", plans, "epub")

    assert calls == ["https://cdnA/big"]  # did not try the second host


# --- _parse_size helper ---

def test_parse_size_kb():
    assert _parse_size("512 Kb") == 512 * 1024


def test_parse_size_mb():
    assert _parse_size("15 Mb") == 15 * 1024 * 1024


def test_parse_size_empty():
    assert _parse_size("") is None


# ===========================================================================
# VK provider tests
# ===========================================================================

import app.providers.vk as _vk_mod


def _vk_response(items):
    return MagicMock(
        status_code=200,
        json=lambda: {"response": {"count": len(items), "items": items}},
    )


def _vk_error_response(code, msg=""):
    return MagicMock(
        status_code=200,
        json=lambda: {"error": {"error_code": code, "error_msg": msg}},
    )


def test_vk_disabled_when_no_token():
    from app.providers.vk import VKProvider
    with patch("app.providers.vk.settings") as mock_cfg:
        mock_cfg.VK_TOKEN = ""
        p = VKProvider()
        assert p.enabled is False


async def test_vk_deprecated_search_returns_empty_even_with_token():
    """VK removed docs.search server-side; provider is hard-disabled.

    Even with a valid token and a mocked client that would return items,
    search must short-circuit to [] and never hit the network."""
    from app.providers.vk import VKProvider

    called = {"hit": False}

    class FakeClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url, params=None, **kw):
            called["hit"] = True
            return _vk_response([{"id": 1, "title": "X", "ext": "epub", "url": "u"}])

    with (
        patch("app.providers.vk.settings") as mock_cfg,
        patch("app.providers.vk.httpx.AsyncClient", FakeClient),
    ):
        mock_cfg.VK_TOKEN = "validtoken"
        mock_cfg.PROVIDER_SEARCH_TIMEOUT_S = 10
        p = VKProvider()
        results = await p.search("book", [])

    assert results == []
    assert called["hit"] is False  # no API call made


def test_vk_disabled_even_with_token():
    """A valid token must not re-enable a deprecated provider."""
    from app.providers.vk import VKProvider
    with patch("app.providers.vk.settings") as mock_cfg:
        mock_cfg.VK_TOKEN = "validtoken"
        p = VKProvider()
        assert p.enabled is False
        assert getattr(p, "deprecated", False) is True


# ===========================================================================
# Anna's Archive provider tests
# ===========================================================================

def _aa_html_page(md5s: list[str]) -> str:
    """Build a minimal AA search HTML with result cards."""
    cards = ""
    for md5 in md5s:
        cards += f"""
        <a href="/md5/{md5}">
          <h3>Book {md5[:4]}</h3>
          <p class="author">Author Name</p>
        </a>
        """
    return f"<html><body>{cards}</body></html>"


async def test_annas_html_search_returns_results():
    from app.providers.annas import AnnasProvider

    html = _aa_html_page(["aabbcc1234567890aabbcc1234567890", "ff00ff1234567890ff00ff1234567890"])

    class FakeResp:
        status_code = 200
        text = html

    class FakeClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url, **kw): return FakeResp()

    with (
        patch("app.providers.annas.settings") as mock_cfg,
        patch("app.providers.annas.httpx.AsyncClient", FakeClient),
    ):
        mock_cfg.AA_API_KEY = ""
        mock_cfg.PROVIDER_SEARCH_TIMEOUT_S = 10
        p = AnnasProvider()
        results = await p.search("test", ["epub"])

    assert len(results) >= 1
    assert all(r.source == "annas" for r in results)
    assert all(r.id.startswith("annas:") for r in results)


async def test_annas_cloudflare_raises():
    from app.providers.annas import AnnasProvider

    class FakeResp:
        status_code = 503
        text = "Just a moment"

    class FakeClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url, **kw): return FakeResp()

    with (
        patch("app.providers.annas.settings") as mock_cfg,
        patch("app.providers.annas.httpx.AsyncClient", FakeClient),
    ):
        mock_cfg.AA_API_KEY = ""
        mock_cfg.PROVIDER_SEARCH_TIMEOUT_S = 10
        p = AnnasProvider()
        with pytest.raises(RuntimeError, match="Cloudflare"):
            await p.search("test", ["epub"])


async def test_annas_resolve_uses_stored_url():
    """When extra has a direct download_url, resolve returns it without HTTP."""
    from app.providers.annas import AnnasProvider
    from app.providers.base import SearchResult

    result = SearchResult(
        id="annas:abc123",
        title="Book",
        ext="epub",
        source="annas",
        extra={"aa_id": "abc123", "download_url": "https://cdn.annas.org/abc.epub"},
    )

    p = AnnasProvider()
    plan = await p.resolve(result)
    assert plan.url == "https://cdn.annas.org/abc.epub"
