"""Libgen provider (.li family). Search via index.php, resolve via ads.php handshake."""
import asyncio
import json
import logging
import re
import time
from urllib.parse import urlencode, urljoin, urlsplit

import httpx
from selectolax.parser import HTMLParser

from app.config import settings
from app.providers.base import DownloadPlan, SearchResult

logger = logging.getLogger("bookhub")

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_CF_SIGNATURES = ("Just a moment", "challenge-platform", "cf_chl")


def _parse_size(size_str: str) -> int | None:
    """Parse Libgen size strings like '512 Kb', '15 Mb', '2.3 Gb' into bytes."""
    m = re.match(r"([\d.,]+)\s*([KMGkmg]?)[Bb]?", size_str.strip())
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
        unit = m.group(2).upper()
        multipliers: dict[str, int] = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}
        return int(num * multipliers.get(unit, 1))
    except (ValueError, KeyError):
        return None


class LibgenProvider:
    name = "libgen"

    def __init__(self) -> None:
        self._mirror_cache: str | None = None
        self._mirror_cached_at: float = 0.0
        self._mirror_ttl: float = 600.0  # 10 min
        self._mirror_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(settings.LIBGEN_MIRRORS)

    def _mirrors(self) -> list[str]:
        return [m.strip() for m in settings.LIBGEN_MIRRORS.split(",") if m.strip()]

    @staticmethod
    def _is_cloudflare(status: int, body: str) -> bool:
        """Detect Cloudflare interstitial by status code or body signatures."""
        if status in (403, 503):
            return True
        return any(sig in body for sig in _CF_SIGNATURES)

    async def _find_mirror(self) -> str | None:
        """Return the first healthy mirror URL, cached for 10 min."""
        async with self._mirror_lock:
            now = time.monotonic()
            if self._mirror_cache and (now - self._mirror_cached_at) < self._mirror_ttl:
                return self._mirror_cache

            mirrors = self._mirrors()
            async with httpx.AsyncClient(
                headers={"User-Agent": _BROWSER_UA},
                follow_redirects=True,
                timeout=5.0,
            ) as client:
                for mirror in mirrors:
                    base = f"https://{mirror}"
                    try:
                        r = await client.get(base + "/")
                        if r.status_code >= 500 or self._is_cloudflare(r.status_code, r.text):
                            logger.info(json.dumps({
                                "kind": "provider",
                                "source": "libgen",
                                "mirror": mirror,
                                "note": f"probe failed: HTTP {r.status_code}",
                            }))
                            continue
                        self._mirror_cache = base
                        self._mirror_cached_at = now
                        return base
                    except Exception as exc:
                        logger.info(json.dumps({
                            "kind": "provider",
                            "source": "libgen",
                            "mirror": mirror,
                            "note": f"probe error: {exc}",
                        }))

            return None

    def _demote_mirror(self) -> None:
        """Force re-probe on next call (used when a real search returns a CF page)."""
        self._mirror_cache = None
        self._mirror_cached_at = 0.0

    async def search(self, query: str, ext_filter: list[str]) -> list[SearchResult]:
        base = await self._find_mirror()
        if not base:
            raise RuntimeError("No Libgen mirror available")

        # covers=on makes libgen include the cover thumbnail <img> in each row.
        url = f"{base}/index.php?{urlencode({'req': query, 'res': 100, 'covers': 'on'})}"
        timeout = settings.PROVIDER_SEARCH_TIMEOUT_S

        async with httpx.AsyncClient(
            headers={"User-Agent": _BROWSER_UA},
            follow_redirects=True,
            timeout=float(timeout),
        ) as client:
            r = await client.get(url)

        if self._is_cloudflare(r.status_code, r.text):
            self._demote_mirror()
            raise RuntimeError(f"Libgen mirror {base} under Cloudflare challenge")

        if r.status_code >= 400:
            raise RuntimeError(f"Libgen search HTTP {r.status_code}")

        return self._parse_results(r.text, base, ext_filter)

    def _parse_results(self, html: str, base: str, ext_filter: list[str]) -> list[SearchResult]:
        """Parse the tablelibgen results table by header name, not column position."""
        tree = HTMLParser(html)
        table = tree.css_first("#tablelibgen")
        if not table:
            return []

        rows = table.css("tr")
        if len(rows) < 2:
            return []

        # Map header names -> column indices (fragment match, case-insensitive).
        # Live libgen uses <th> header cells; fall back to <td> for older layouts/fixtures.
        header_cells = rows[0].css("th") or rows[0].css("td")
        headers = [c.text().strip().lower() for c in header_cells]

        def col_idx(fragment: str) -> int:
            fl = fragment.lower()
            for i, h in enumerate(headers):
                if fl in h:
                    return i
            return -1

        title_idx = col_idx("title")
        author_idx = col_idx("author")
        ext_idx = col_idx("ext")
        size_idx = col_idx("size")
        mirrors_idx = col_idx("mirrors")

        if title_idx < 0 or ext_idx < 0:
            return []

        results = []
        for row in rows[1:]:
            cells = row.css("td")
            if not cells:
                continue

            def cell_text(idx: int) -> str:
                return cells[idx].text().strip() if 0 <= idx < len(cells) else ""

            ext = cell_text(ext_idx).lower()
            if ext not in ("epub", "pdf"):
                continue
            if ext_filter and ext not in ext_filter:
                continue

            md5 = self._extract_md5(cells, mirrors_idx, title_idx)
            if not md5:
                continue

            title = ""
            if 0 <= title_idx < len(cells):
                # Pick the first link with non-empty text (skips cover/thumbnail anchors).
                for link in cells[title_idx].css("a"):
                    lt = link.text().strip()
                    if lt:
                        title = lt
                        break
                if not title:
                    title = cells[title_idx].text().strip()

            author = cell_text(author_idx) if author_idx >= 0 else ""
            size_bytes = _parse_size(cell_text(size_idx)) if size_idx >= 0 else None
            cover_url = self._extract_cover(row, base)

            results.append(SearchResult(
                id=f"libgen:{md5}",
                title=title,
                author=author or None,
                ext=ext,
                size_bytes=size_bytes,
                source="libgen",
                cover_url=cover_url,
                extra={"md5": md5, "base": base},
            ))

        return results

    @staticmethod
    def _extract_cover(row, base: str) -> str | None:
        """Pull the cover thumbnail URL from a result row, made absolute.

        Live libgen rows carry a small <img> (often lazy-loaded via data-src).
        Returns None when the row has no usable cover.
        """
        img = row.css_first("img")
        if img is None:
            return None
        src = (
            img.attributes.get("data-src")
            or img.attributes.get("data-original")
            or img.attributes.get("src")
            or ""
        ).strip()
        # Skip inline placeholders (blank.png, data: URIs, 1px spacers).
        if not src or src.startswith("data:") or "blank" in src.lower():
            return None
        return urljoin(base + "/", src)

    @staticmethod
    def _extract_md5(cells, mirrors_idx: int, title_idx: int) -> str | None:
        """Extract MD5 from the mirrors or title column's href."""
        for idx in (mirrors_idx, title_idx):
            if not (0 <= idx < len(cells)):
                continue
            for link in cells[idx].css("a"):
                href = link.attributes.get("href") or ""
                m = re.search(r"(?i)md5=([a-f0-9]{32})", href)
                if m:
                    return m.group(1).lower()
        return None

    async def resolve_candidates(self, result: SearchResult) -> list[DownloadPlan]:
        """Resolve every distinct download URL across mirrors.

        Each mirror's ads.php hands out a get.php link on some CDN host. A host
        that 503s our server IP keeps 503ing regardless of the key, so we gather
        one plan per *distinct host* and let the downloader fall back across
        them. Hitting all mirrors costs a few extra ads.php requests at resolve
        time, but makes the download robust to a single dead CDN.
        """
        md5 = result.extra.get("md5")
        if not md5:
            raise RuntimeError(f"No MD5 for Libgen result {result.id}")

        # Candidate mirrors: the one the result came from first, then the rest.
        preferred = result.extra.get("base")
        mirror_bases: list[str] = []
        for m in ([preferred] if preferred else []) + [
            f"https://{m.strip()}" for m in settings.LIBGEN_MIRRORS.split(",") if m.strip()
        ]:
            if m and m not in mirror_bases:
                mirror_bases.append(m)
        if not mirror_bases:
            raise RuntimeError("No Libgen mirror available for resolve")

        timeout = settings.PROVIDER_RESOLVE_TIMEOUT_S
        last_err = "no mirrors tried"
        plans: list[DownloadPlan] = []
        seen_hosts: set[str] = set()

        async with httpx.AsyncClient(
            headers={"User-Agent": _BROWSER_UA},
            follow_redirects=True,
            timeout=float(timeout),
        ) as client:
            for base in mirror_bases:
                ads_url = f"{base}/ads.php?md5={md5}"
                try:
                    r = await client.get(ads_url)
                except Exception as exc:
                    last_err = f"ads.php request failed on {base}: {exc}"
                    continue
                if r.status_code >= 400 or self._is_cloudflare(r.status_code, r.text):
                    last_err = f"ads.php HTTP {r.status_code} on {base}"
                    continue
                get_url = self._extract_get_url(r.text, ads_url)
                if not get_url:
                    last_err = f"no get.php key link on {base}"
                    continue
                host = urlsplit(get_url).netloc
                if host in seen_hosts:
                    continue
                seen_hosts.add(host)
                plans.append(DownloadPlan(
                    url=get_url,
                    headers={"Referer": ads_url},
                    cookies=dict(r.cookies),
                ))

        if not plans:
            raise RuntimeError(f"Libgen resolve failed for md5={md5}: {last_err}")
        return plans

    async def resolve(self, result: SearchResult) -> DownloadPlan:
        """First available candidate (kept for the single-plan Provider API)."""
        return (await self.resolve_candidates(result))[0]

    @staticmethod
    def _extract_get_url(html: str, ads_url: str) -> str | None:
        """Find the get.php?md5=...&key=... link in ads.php HTML."""
        tree = HTMLParser(html)
        for link in tree.css("a"):
            # selectolax returns None for a valueless attribute (e.g. <a href>).
            href = link.attributes.get("href") or ""
            if "get.php" in href and "key=" in href:
                if not href.startswith("http"):
                    href = urljoin(ads_url, href)
                return href
        return None
