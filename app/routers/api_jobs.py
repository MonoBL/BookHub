"""Job routes: POST /api/download, GET /api/jobs/{id}. See BUILD.md §7.1."""
import asyncio
import uuid
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import require_user
from app.models import Job
from app.providers import PROVIDERS
from app.providers.base import SearchResult
from app.services import jobs as job_svc
from app.services.downloader import download
from app.services.scanner import verify_and_scan

router = APIRouter()

_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class DownloadRequest(BaseModel):
    result: dict  # SearchResult as dict


@router.post("/download")
async def start_download(
    body: DownloadRequest,
    user: dict = Depends(require_user),
):
    result = SearchResult(**body.result)
    ext = result.ext.lower()
    if ext not in ("epub", "pdf"):
        raise HTTPException(status_code=400, detail="Only EPUB and PDF supported")

    job_id = str(uuid.uuid4())
    job = Job(id=job_id, status="queued", ext=ext, title=result.title)
    await job_svc.create_job(job)

    # Find the provider that owns this result.
    provider = next((p for p in PROVIDERS if p.name == result.source and p.enabled), None)
    if not provider:
        await job_svc.update_job(job_id, status="error", reason=f"provider '{result.source}' not available")
        return {"job_id": job_id}

    asyncio.create_task(_run_pipeline(job_id, result, provider, ext))
    return {"job_id": job_id}


async def _run_pipeline(job_id: str, result: SearchResult, provider, ext: str) -> None:
    try:
        plan = await provider.resolve(result)
        path = await download(job_id, plan, ext)
        await verify_and_scan(job_id, path, ext)
    except Exception as exc:
        current = await job_svc.get_job(job_id)
        if current and current.status not in ("blocked", "unverified", "clean"):
            await job_svc.update_job(job_id, status="error", reason=str(exc))


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, user: dict = Depends(require_user)):
    if not _UUID4_RE.match(job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID format")
    job = await job_svc.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.model_dump()
