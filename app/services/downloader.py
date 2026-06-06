# Downloader service - implemented in M4.
from app.providers.base import SearchResult


async def download(job_id: str, result: SearchResult) -> None:
    raise NotImplementedError("Downloader not yet implemented")
