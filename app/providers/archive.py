"""Internet Archive (archive.org) provider.

Search via advancedsearch.php (returns items, not files); resolve by fetching an
item's /metadata/<id> and picking the actual EPUB/PDF files. archive.org has no
Cloudflare gate and a clean public API, and carries a lot of Portuguese-language
titles, so it complements the libgen/annas mirrors. See the example item
archive.org/details/RapidoEDevagarDuasFormasDePensar (two EPUBs in one item).
"""
import asyncio
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
        # the item is indexed as carrying at least one file we can hand back.
        # No sort -> relevance ranking, so a specific title isn't buried under
        # high-download but loosely-matching items within the row cap.
        q = f"({query}) AND mediatype:texts AND format:({fmt_clause})"
        params = [
            ("q", q),
            ("fl[]", "identifier"),
            ("fl[]", "title"),
            ("fl[]", "creator"),
            ("fl[]", "format"),
            ("rows", str(settings.ARCHIVE_ROWS)),
            ("page", "1"),
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
            results = self._parse_docs(docs, ext_filter)
            # Fill in real per-file sizes (the search index only has whole-item
            # size). One /metadata call per item, fanned out and capped; this
            # also drops rows whose indexed format has no actual matching file.
            return await self._enrich_sizes(results, client)

    async def _enrich_sizes(self, results, client) -> list[SearchResult]:
        """Fetch /metadata per unique item, attach matching-file sizes + URLs.

        Stores the resolved file list in extra["files"] (smallest first) so
        resolve_candidates needs no second network call. A metadata failure
        keeps the row with size unknown (resolve falls back to a live fetch).
        """
        identifiers = {r.extra["identifier"] for r in results}
        sem = asyncio.Semaphore(settings.ARCHIVE_METADATA_CONCURRENCY)

        async def fetch(identifier: str) -> tuple[str, list[dict]]:
            async with sem:
                try:
                    mr = await client.get(f"{_BASE}/metadata/{quote(identifier, safe='')}")
                    if mr.status_code >= 400:
                        return identifier, []
                    return identifier, mr.json().get("files", [])
                except Exception:
                    return identifier, []

        meta = dict(await asyncio.gather(*(fetch(i) for i in identifiers)))

        enriched: list[SearchResult] = []
        for r in results:
            files = meta.get(r.extra["identifier"]) or []
            matched = [
                {"name": f["name"], "size": _file_size(f)}
                for f in files
                if _file_ext(f) == r.ext and f.get("name")
            ]
            # A non-empty file list that yields no match means the indexed
            # format had no real file -> drop. An empty list means the metadata
            # fetch failed; keep the row and let resolve fetch live.
            if files and not matched:
                continue
            matched.sort(key=lambda f: f["size"] if f["size"] is not None else 1 << 62)
            update = {"extra": {**r.extra, "files": matched}}
            if matched and matched[0]["size"] is not None:
                update["size_bytes"] = matched[0]["size"]
            enriched.append(r.model_copy(update=update))
        return enriched

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
        """Return one plan per matching file, smallest first.

        An item often holds several files of the same ext (e.g. a 137 MB scanned
        EPUB next to a 1 MB reflowable one). Smallest-first puts the real text
        edition ahead of the giant scan and keeps the first candidate under the
        download size cap. The downloader falls back across the list.

        Search already enriched extra["files"] (name + size, smallest first) via
        /metadata, so the common path needs no network call here; we only fetch
        live when that list is missing (e.g. a stale job replayed after restart).
        """
        identifier = result.extra.get("identifier")
        if not identifier:
            raise RuntimeError(f"No identifier for archive result {result.id}")

        files = result.extra.get("files")
        if files is None:
            files = await self._fetch_matching_files(identifier, result.ext)
        if not files:
            raise RuntimeError(f"archive.org: no {result.ext} file in {identifier}")

        plans: list[DownloadPlan] = []
        for f in files:
            dl_url = f"{_BASE}/download/{quote(identifier, safe='')}/{quote(f['name'])}"
            plans.append(DownloadPlan(
                url=dl_url,
                headers={
                    "Referer": f"{_BASE}/details/{identifier}",
                    "User-Agent": _BROWSER_UA,
                },
                size_bytes=f.get("size"),
            ))
        return plans

    async def _fetch_matching_files(self, identifier: str, ext: str) -> list[dict]:
        """Live /metadata fetch -> matching files [{name, size}], smallest first."""
        meta_url = f"{_BASE}/metadata/{quote(identifier, safe='')}"
        async with httpx.AsyncClient(
            headers={"User-Agent": _BROWSER_UA},
            follow_redirects=True,
            timeout=float(settings.PROVIDER_RESOLVE_TIMEOUT_S),
        ) as client:
            r = await client.get(meta_url)

        if r.status_code >= 400:
            raise RuntimeError(f"archive.org metadata HTTP {r.status_code}")

        matched = [
            {"name": f["name"], "size": _file_size(f)}
            for f in r.json().get("files", [])
            if _file_ext(f) == ext and f.get("name")
        ]
        matched.sort(key=lambda f: f["size"] if f["size"] is not None else 1 << 62)
        return matched

    async def resolve(self, result: SearchResult) -> DownloadPlan:
        return (await self.resolve_candidates(result))[0]
