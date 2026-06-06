"""Anna's Archive provider. Uses JSON API (donor key) or HTML scraping."""
import logging
import re
from urllib.parse import urljoin, urlencode, quote

import httpx
from selectolax.parser import HTMLParser

from app.config import settings
from app.providers.base import DownloadPlan, SearchResult

log = logging.getLogger("bookhub")

_AA_BASE = "https://annas-archive.org"
_CF_SIGNATURES = ("Just a moment", "challenge-platform", "cf_chl")
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _is_cloudflare(status: int, body: str) -> bool:
    if status in (403, 503):
        return True
    return any(sig in body for sig in _CF_SIGNATURES)


class AnnasProvider:
    name = "annas"

    @property
    def enabled(self) -> bool:
        return True

    async def search(self, query: str, ext_filter: list[str]) -> list[SearchResult]:
        if settings.AA_API_KEY:
            return await self._search_api(query, ext_filter)
        return await self._search_html(query, ext_filter)

    async def _search_api(self, query: str, ext_filter: list[str]) -> list[SearchResult]:
        """Use donor JSON API."""
        exts = ext_filter if ext_filter else ["epub", "pdf"]
        results = []
        timeout = float(settings.PROVIDER_SEARCH_TIMEOUT_S)

        for ext in exts:
            params = {
                "q": query,
                "ext": ext,
                "limit": 20,
            }
            headers = {
                "X-AA-API-Key": settings.AA_API_KEY,
                "User-Agent": _BROWSER_UA,
            }
            try:
                async with httpx.AsyncClient(
                    timeout=timeout, follow_redirects=True, headers=headers
                ) as client:
                    r = await client.get(f"{_AA_BASE}/api/search", params=params)
                if r.status_code >= 400:
                    log.warning("AA API search HTTP %s", r.status_code)
                    continue
                data = r.json()
                for item in data.get("results", []):
                    results.append(self._api_item_to_result(item, ext))
            except Exception as exc:
                log.warning("AA API search error: %s", exc)

        return [r for r in results if r is not None]

    def _api_item_to_result(self, item: dict, ext: str) -> SearchResult | None:
        aa_id = item.get("md5") or item.get("id")
        if not aa_id:
            return None
        return SearchResult(
            id=f"annas:{aa_id}",
            title=(item.get("title") or "").strip() or "Untitled",
            author=(item.get("author") or None),
            ext=ext,
            size_bytes=item.get("filesize"),
            source="annas",
            extra={"aa_id": aa_id, "download_url": item.get("download_url")},
        )

    async def _search_html(self, query: str, ext_filter: list[str]) -> list[SearchResult]:
        """HTML scraping fallback (no API key)."""
        exts = ext_filter if ext_filter else ["epub", "pdf"]
        results = []
        timeout = float(settings.PROVIDER_SEARCH_TIMEOUT_S)

        for ext in exts:
            params = urlencode({"q": query, "ext": ext, "sort": ""})
            url = f"{_AA_BASE}/search?{params}"
            try:
                async with httpx.AsyncClient(
                    timeout=timeout,
                    follow_redirects=True,
                    headers={"User-Agent": _BROWSER_UA},
                ) as client:
                    r = await client.get(url)

                if _is_cloudflare(r.status_code, r.text):
                    log.warning("Anna's Archive: Cloudflare challenge on search")
                    raise RuntimeError("Anna's Archive under Cloudflare challenge")

                if r.status_code >= 400:
                    raise RuntimeError(f"AA search HTTP {r.status_code}")

                results.extend(self._parse_html_results(r.text, ext, ext_filter))
            except RuntimeError:
                raise
            except Exception as exc:
                log.warning("AA HTML search error: %s", exc)

        return results

    def _parse_html_results(
        self, html: str, ext: str, ext_filter: list[str]
    ) -> list[SearchResult]:
        tree = HTMLParser(html)
        results = []

        # Anna's Archive result cards have an href like /md5/<hash>
        for link in tree.css("a[href*='/md5/']"):
            href = link.attributes.get("href", "")
            m = re.search(r"/md5/([a-f0-9]{32})", href)
            if not m:
                continue
            aa_id = m.group(1)

            # Extract title: first non-empty text node or inner heading
            title_el = link.css_first("h3") or link.css_first("[class*=title]")
            title = (title_el.text(strip=True) if title_el else link.text(strip=True)) or "Untitled"

            # Author: look for author-like element
            author_el = link.css_first("[class*=author]") or link.css_first("p")
            author = author_el.text(strip=True) if author_el else None

            results.append(SearchResult(
                id=f"annas:{aa_id}",
                title=title,
                author=author or None,
                ext=ext,
                size_bytes=None,
                source="annas",
                extra={"aa_id": aa_id},
            ))

        return results

    async def resolve(self, result: SearchResult) -> DownloadPlan:
        """Resolve an AA result to a direct download URL."""
        aa_id = result.extra.get("aa_id") or result.extra.get("download_url")
        if not aa_id:
            raise RuntimeError(f"No AA ID for result {result.id}")

        # If we already have a direct download URL from the API, use it.
        direct = result.extra.get("download_url")
        if direct and direct.startswith("http"):
            return DownloadPlan(url=direct)

        # Otherwise navigate to the MD5 page and find the fastest download link.
        timeout = float(settings.PROVIDER_RESOLVE_TIMEOUT_S)
        md5_url = f"{_AA_BASE}/md5/{aa_id}"

        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": _BROWSER_UA},
        ) as client:
            r = await client.get(md5_url)

        if _is_cloudflare(r.status_code, r.text):
            raise RuntimeError("Anna's Archive: Cloudflare challenge on resolve")

        if r.status_code >= 400:
            raise RuntimeError(f"AA MD5 page HTTP {r.status_code}")

        # Find a direct download link (look for /fast_download or /download links).
        tree = HTMLParser(r.text)
        for link in tree.css("a[href]"):
            href = link.attributes.get("href", "")
            if any(kw in href for kw in ("/fast_download", "/download", "/slow_download")):
                if not href.startswith("http"):
                    href = urljoin(_AA_BASE, href)
                return DownloadPlan(url=href, headers={"Referer": md5_url})

        raise RuntimeError(f"AA: no download link found on {md5_url}")
