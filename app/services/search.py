"""Search service: fan-out, dedup, per-provider status, result cache. See BUILD.md §6.2."""
import asyncio
import json
import logging
import re
import time
from typing import Any

from app.config import settings
from app.providers import PROVIDERS
from app.providers.base import ProviderStatus, SearchResult

logger = logging.getLogger("bookhub")

# Simple in-memory result cache: (provider_name, query, ext_key) -> (results, ts)
_cache: dict[tuple, tuple[list[SearchResult], float]] = {}
_CACHE_TTL = 300.0  # 5 minutes


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation and extra whitespace for dedup keys."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", text.lower())).strip()


def _dedup_results(all_results: list[SearchResult]) -> list[SearchResult]:
    """
    Cross-source dedup: group by normalize(title)+normalize(author)+ext.
    Within Libgen, dedup by md5 (authoritative).
    Returns one row per unique book; extra.sources collects all provider names.
    """
    seen_md5: set[str] = set()
    groups: dict[str, SearchResult] = {}

    for r in all_results:
        # Within Libgen, dedup by md5.
        md5 = r.extra.get("md5")
        if r.source == "libgen" and md5:
            if md5 in seen_md5:
                continue
            seen_md5.add(md5)

        key = f"{_normalize(r.title)}|{_normalize(r.author or '')}|{r.ext}"
        if key in groups:
            # Merge: add this result as an alternative source.
            existing = groups[key]
            sources = existing.extra.get("sources", [existing.source])
            if r.source not in sources:
                sources.append(r.source)
            groups[key] = existing.model_copy(
                update={"extra": {**existing.extra, "sources": sources}}
            )
        else:
            groups[key] = r.model_copy(
                update={"extra": {**r.extra, "sources": [r.source]}}
            )

    return list(groups.values())


async def _search_provider(provider, query: str, ext_filter: list[str]) -> tuple[list[SearchResult], ProviderStatus]:
    cache_key = (provider.name, _normalize(query), ",".join(sorted(ext_filter)))
    now = time.monotonic()

    if cache_key in _cache:
        results, cached_at = _cache[cache_key]
        if now - cached_at < _CACHE_TTL:
            return results, ProviderStatus(name=provider.name, status="ok", count=len(results), note="cached")

    try:
        results = await asyncio.wait_for(
            provider.search(query, ext_filter),
            timeout=float(settings.PROVIDER_SEARCH_TIMEOUT_S),
        )
        _cache[cache_key] = (results, now)
        return results, ProviderStatus(name=provider.name, status="ok", count=len(results))
    except asyncio.TimeoutError:
        logger.info(json.dumps({"kind": "provider", "source": provider.name, "note": "search timeout"}))
        return [], ProviderStatus(name=provider.name, status="timeout", note="search timed out")
    except Exception as exc:
        logger.info(json.dumps({"kind": "provider", "source": provider.name, "note": str(exc)}))
        return [], ProviderStatus(name=provider.name, status="error", note=str(exc))


async def search(query: str, ext_filter: list[str]) -> dict[str, Any]:
    """Fan out to all enabled providers, dedup results, return shape for API."""
    enabled = [p for p in PROVIDERS if p.enabled]
    disabled = [p for p in PROVIDERS if not p.enabled]

    tasks = [_search_provider(p, query, ext_filter) for p in enabled]
    outcomes = await asyncio.gather(*tasks, return_exceptions=False)

    all_results: list[SearchResult] = []
    statuses: list[ProviderStatus] = []

    for outcome in outcomes:
        results, status = outcome
        all_results.extend(results)
        statuses.append(status)

    for p in disabled:
        statuses.append(ProviderStatus(name=p.name, status="disabled"))

    deduped = _dedup_results(all_results)

    return {
        "results": [r.model_dump() for r in deduped],
        "providers": [s.model_dump() for s in statuses],
    }
