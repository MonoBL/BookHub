# Anna's Archive provider - implemented in M7.
from app.providers.base import SearchResult, DownloadPlan
from app.config import settings


class AnnasProvider:
    name = "annas"

    @property
    def enabled(self) -> bool:
        return True

    async def search(self, query: str, ext_filter: list[str]) -> list[SearchResult]:
        return []

    async def resolve(self, result: SearchResult) -> DownloadPlan:
        raise NotImplementedError("Anna's Archive provider not yet implemented")
