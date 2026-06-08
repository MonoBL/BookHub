"""Internet Archive (archive.org) provider.

Search via advancedsearch.php (returns items, not files); resolve by fetching an
item's /metadata/<id> and picking the actual EPUB/PDF files. archive.org has no
Cloudflare gate and a clean public API, and carries a lot of Portuguese-language
titles, so it complements the libgen/annas mirrors. See the example item
archive.org/details/RapidoEDevagarDuasFormasDePensar (two EPUBs in one item).
"""
import logging
from urllib.parse import quote, urlencode

import httpx

from app.config import settings
from app.providers.base import DownloadPlan, SearchResult

log = logging.getLogger("bookhub")

_BASE = "https://archive.org"
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# archive.org index `format` values that map to a usable ext. PDF appears under
# several format labels; EPUB is always just "EPUB".
_PDF_FORMATS = ("text pdf", "image container pdf", "additional text pdf")


def _exts_to_formats(ext_filter: list[str]) -> str:
    """Build the format:() clause for advancedsearch from the ext filter."""
    exts = ext_filter or ["epub", "pdf"]
    clauses: list[str] = []
    if "epub" in exts:
        clauses.append('"EPUB"')
    if "pdf" in exts:
        clauses.append('"Text PDF"')
    return " OR ".join(clauses)


def _normalize_formats(raw) -> list[str]:
    """advancedsearch returns `format` as a str or list; normalize to lowercase list."""
    if isinstance(raw, list):
        return [str(f).lower() for f in raw]
    if isinstance(raw, str):
        return [raw.lower()]
    return []


def _file_ext(f: dict) -> str | None:
    """Classify a metadata file entry as 'epub', 'pdf', or None."""
    fmt = (f.get("format") or "").lower()
    name = (f.get("name") or "").lower()
    if fmt == "epub" or name.endswith(".epub"):
        return "epub"
    if fmt in _PDF_FORMATS or name.endswith(".pdf"):
        return "pdf"
    return None


def _file_size(f: dict) -> int | None:
    try:
        return int(f.get("size"))
    except (TypeError, ValueError):
        return None


class ArchiveProvider:
    name = "archive"

    @property
    def enabled(self) -> bool:
        return settings.ARCHIVE_ENABLED

    async def search(self, query: str, ext_filter: list[str]) -> list[SearchResult]:
        fmt_clause = _exts_to_formats(ext_filter)
        # mediatype:texts keeps us out of audio/video/software; format:() ensures
        # the item actually carries at least one file we can hand back.
        q = f"({query}) AND mediatype:texts AND format:({fmt_clause})"
        params = [
            ("q", q),
            ("fl[]", "identifier"),
            ("fl[]", "title"),
            ("fl[]", "creator"),
            ("fl[]", "format"),
            ("rows", str(settings.ARCHIVE_ROWS)),
            ("page", "1"),
            ("sort[]", "downloads desc"),
            ("output", "json"),
        ]
        url = f"{_BASE}/advancedsearch.php?{urlencode(params)}"

        async with httpx.AsyncClient(
            headers={"User-Agent": _BROWSER_UA},
            follow_redirects=True,
            timeout=float(settings.PROVIDER_SEARCH_TIMEOUT_S),
        ) as client:
            r = await client.get(url)

        if r.status_code >= 400:
            raise RuntimeError(f"archive.org search HTTP {r.status_code}")

        docs = r.json().get("response", {}).get("docs", [])
        return self._parse_docs(docs, ext_filter)

    def _parse_docs(self, docs: list[dict], ext_filter: list[str]) -> list[SearchResult]:
        wanted = set(ext_filter or ["epub", "pdf"])
        results: list[SearchResult] = []

        for doc in docs:
            identifier = doc.get("identifier")
            if not identifier:
                continue

            formats = _normalize_formats(doc.get("format"))
            present: list[str] = []
            if "epub" in formats:
                present.append("epub")
            if any(f in _PDF_FORMATS for f in formats):
                present.append("pdf")

            title = (doc.get("title") or "").strip() or "Untitled"
            # creator may be a list when an item has multiple authors.
            creator = doc.get("creator")
            if isinstance(creator, list):
                creator = ", ".join(str(c) for c in creator) or None
            author = (creator or "").strip() or None if creator else None

            # One row per available ext that the caller asked for. The actual
            # file (and its size) is only known after /metadata at resolve time.
            for ext in present:
                if ext not in wanted:
                    continue
                results.append(SearchResult(
                    id=f"archive:{identifier}:{ext}",
                    title=title,
                    author=author,
                    ext=ext,
                    size_bytes=None,
                    source="archive",
                    cover_url=f"{_BASE}/services/img/{quote(identifier, safe='')}",
                    extra={"identifier": identifier},
                ))

        return results

    async def resolve_candidates(self, result: SearchResult) -> list[DownloadPlan]:
        """Fetch item metadata and return one plan per matching file, smallest first.

        An item often holds several files of the same ext (e.g. a 137 MB scanned
        EPUB next to a 1 MB reflowable one). Smallest-first puts the real text
        edition ahead of the giant scan and keeps the first candidate under the
        download size cap. The downloader falls back across the list.
        """
        identifier = result.extra.get("identifier")
        if not identifier:
            raise RuntimeError(f"No identifier for archive result {result.id}")

        meta_url = f"{_BASE}/metadata/{quote(identifier, safe='')}"
        async with httpx.AsyncClient(
            headers={"User-Agent": _BROWSER_UA},
            follow_redirects=True,
            timeout=float(settings.PROVIDER_RESOLVE_TIMEOUT_S),
        ) as client:
            r = await client.get(meta_url)

        if r.status_code >= 400:
            raise RuntimeError(f"archive.org metadata HTTP {r.status_code}")

        files = r.json().get("files", [])
        matches = [
            f for f in files
            if _file_ext(f) == result.ext and f.get("name")
        ]
        if not matches:
            raise RuntimeError(f"archive.org: no {result.ext} file in {identifier}")

        # Smallest first (unknown sizes sort last so real files win).
        matches.sort(key=lambda f: _file_size(f) if _file_size(f) is not None else 1 << 62)

        plans: list[DownloadPlan] = []
        for f in matches:
            dl_url = f"{_BASE}/download/{quote(identifier, safe='')}/{quote(f['name'])}"
            plans.append(DownloadPlan(
                url=dl_url,
                headers={"Referer": f"{_BASE}/details/{identifier}"},
                size_bytes=_file_size(f),
            ))
        return plans

    async def resolve(self, result: SearchResult) -> DownloadPlan:
        return (await self.resolve_candidates(result))[0]
