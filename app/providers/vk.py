# VK provider - implemented in M7.
from app.providers.base import SearchResult, DownloadPlan
from app.config import settings


class VKProvider:
    name = "vk"

    @property
    def enabled(self) -> bool:
        return bool(settings.VK_TOKEN)

    async def search(self, query: str, ext_filter: list[str]) -> list[SearchResult]:
        return []

    async def resolve(self, result: SearchResult) -> DownloadPlan:
        raise NotImplementedError("VK provider not yet implemented")
