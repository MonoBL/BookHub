# Libgen provider (.li family) - implemented in M3.
from app.providers.base import SearchResult, DownloadPlan
from app.config import settings


class LibgenProvider:
    name = "libgen"

    @property
    def enabled(self) -> bool:
        return bool(settings.LIBGEN_MIRRORS)

    async def search(self, query: str, ext_filter: list[str]) -> list[SearchResult]:
        return []

    async def resolve(self, result: SearchResult) -> DownloadPlan:
        raise NotImplementedError("Libgen provider not yet implemented")
