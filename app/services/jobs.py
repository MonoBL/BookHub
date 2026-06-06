# In-memory job registry - implemented in M4.
import asyncio
from app.models import Job

_lock = asyncio.Lock()
_jobs: dict[str, Job] = {}


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
