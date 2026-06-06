# Search service - implemented in M5.
from app.providers.base import SearchResult, ProviderStatus


async def search(query: str, ext_filter: list[str]) -> dict:
    return {"results": [], "providers": []}
