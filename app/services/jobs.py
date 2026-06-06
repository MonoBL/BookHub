"""In-memory job registry. Guarded by one asyncio.Lock. See BUILD.md §7.1."""
import asyncio
from app.models import Job

_lock = asyncio.Lock()
_jobs: dict[str, Job] = {}
_in_flight: set[str] = set()  # job_ids currently being served (TTL cleaner skips these)


async def create_job(job: Job) -> None:
    async with _lock:
        _jobs[job.id] = job


async def get_job(job_id: str) -> Job | None:
    async with _lock:
        return _jobs.get(job_id)


async def update_job(job_id: str, **kwargs) -> None:
    async with _lock:
        job = _jobs.get(job_id)
        if job:
            _jobs[job_id] = job.model_copy(update=kwargs)


async def remove_job(job_id: str) -> None:
    async with _lock:
        _jobs.pop(job_id, None)


async def add_in_flight(job_id: str) -> None:
    async with _lock:
        _in_flight.add(job_id)


async def remove_in_flight(job_id: str) -> None:
    async with _lock:
        _in_flight.discard(job_id)


async def is_in_flight(job_id: str) -> bool:
    async with _lock:
        return job_id in _in_flight


async def all_jobs_snapshot() -> dict[str, Job]:
    """Return a copy of the registry for the cleaner."""
    async with _lock:
        return dict(_jobs)


async def in_flight_snapshot() -> set[str]:
    async with _lock:
        return set(_in_flight)
