"""VK Documents provider. DEPRECATED.

VK removed the `docs.search` API method (returns error 3 "Unknown method
passed"), so global document search on VK is no longer possible. A valid token
still authenticates and `docs.get` works, but only lists the token owner's own
uploads, which is useless for finding books. The provider is therefore hard
-disabled: it never participates in search regardless of whether VK_TOKEN is
set. Kept only so historical jobs/configs don't break imports. See BUILD.md §6.
"""
import logging

import httpx

from app.config import settings
from app.providers.base import DownloadPlan, SearchResult

log = logging.getLogger("bookhub")

_VK_API = "https://api.vk.com/method"
_VK_VERSION = "5.199"

# Hard kill-switch: docs.search no longer exists server-side (2024+).
_VK_DEPRECATED = True

# Process-lifetime flag: set True on auth error (code 5 or 15) so we stop retrying.
_vk_disabled = False


def _token() -> str:
    """Return active VK token (DB override not yet wired; env only for now)."""
    return settings.VK_TOKEN


class VKProvider:
    name = "vk"
    deprecated = True

    @property
    def enabled(self) -> bool:
        # Never enabled: VK killed docs.search. See module docstring.
        if _VK_DEPRECATED:
            return False
        return bool(_token()) and not _vk_disabled

    async def search(self, query: str, ext_filter: list[str]) -> list[SearchResult]:
        global _vk_disabled
        if _VK_DEPRECATED:
            return []
        token = _token()
        if not token:
            return []

        params = {
            "q": query,
            "count": 50,
            "access_token": token,
            "v": _VK_VERSION,
        }

        async with httpx.AsyncClient(timeout=float(settings.PROVIDER_SEARCH_TIMEOUT_S)) as client:
            r = await client.get(f"{_VK_API}/docs.search", params=params)
        r.raise_for_status()
        data = r.json()

        if "error" in data:
            code = data["error"].get("error_code")
            msg = data["error"].get("error_msg", "")
            if code in (5, 15):
                _vk_disabled = True
                log.warning("VK provider disabled permanently: error %s - %s", code, msg)
                return []
            raise RuntimeError(f"VK API error {code}: {msg}")

        items = data.get("response", {}).get("items", [])
        results = []
        for item in items:
            ext = (item.get("ext") or "").lower()
            if ext not in ("epub", "pdf"):
                continue
            if ext_filter and ext not in ext_filter:
                continue
            url = item.get("url", "")
            if not url:
                continue
            results.append(SearchResult(
                id=f"vk:{item['id']}",
                title=item.get("title", "").strip() or "Untitled",
                author=None,
                ext=ext,
                size_bytes=item.get("size"),
                source="vk",
                extra={"url": url, "vk_id": item.get("id")},
            ))
        return results

    async def resolve(self, result: SearchResult) -> DownloadPlan:
        url = result.extra.get("url", "")
        if url:
            # Quick HEAD to verify the URL is still alive.
            try:
                async with httpx.AsyncClient(
                    timeout=float(settings.PROVIDER_RESOLVE_TIMEOUT_S),
                    follow_redirects=True,
                ) as client:
                    r = await client.head(url)
                if r.status_code < 400:
                    return DownloadPlan(url=url)
            except Exception:
                pass  # fall through to re-search

        # Re-search by title to get a fresh URL.
        title = result.title
        fresh = await self.search(title, [result.ext])
        for candidate in fresh:
            if candidate.extra.get("vk_id") == result.extra.get("vk_id"):
                fresh_url = candidate.extra.get("url", "")
                if fresh_url:
                    return DownloadPlan(url=fresh_url)

        raise RuntimeError(f"VK: could not refresh download URL for {result.id}")
