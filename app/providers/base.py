from typing import Protocol, runtime_checkable
from pydantic import BaseModel


class SearchResult(BaseModel):
    id: str
    title: str
    author: str | None = None
    ext: str
    size_bytes: int | None = None
    source: str
    cover_url: str | None = None  # absolute URL on a provider host; served via /api/cover proxy
    extra: dict = {}


class ProviderStatus(BaseModel):
    name: str
    status: str   # 'ok' | 'error' | 'timeout' | 'disabled'
    count: int = 0
    note: str | None = None


class DownloadPlan(BaseModel):
    url: str
    headers: dict = {}
    cookies: dict = {}


@runtime_checkable
class Provider(Protocol):
    name: str
    enabled: bool

    async def search(self, query: str, ext_filter: list[str]) -> list[SearchResult]: ...
    async def resolve(self, result: SearchResult) -> DownloadPlan: ...
