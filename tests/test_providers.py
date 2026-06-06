"""M3 provider tests: Libgen search parsing, CF detection, filter, mirror fallback."""
from pathlib import Path
from unittest.mock import patch

import pytest

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


# --- _parse_size helper ---

def test_parse_size_kb():
    assert _parse_size("512 Kb") == 512 * 1024


def test_parse_size_mb():
    assert _parse_size("15 Mb") == 15 * 1024 * 1024


def test_parse_size_empty():
    assert _parse_size("") is None
