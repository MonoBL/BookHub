"""Job routes: POST /api/download, GET /api/jobs/{id}, GET /api/history,
GET /api/downloads. See BUILD.md §7.1."""
import asyncio
import uuid
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import require_user
from app.config import settings
from app.db import get_db, write_db
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
    job = Job(
        id=job_id,
        status="queued",
        ext=ext,
        title=result.title,
        user_id=user["id"],
        source=result.source,
        author=result.author,
    )
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

    # IDOR: non-admin users can only see their own jobs.
    if job.user_id is not None and job.user_id != user["id"] and not user.get("is_admin"):
        raise HTTPException(status_code=404, detail="Job not found")

    return job.model_dump()


@router.get("/downloads")
async def pending_downloads(user: dict = Depends(require_user)):
    """A user's ready-but-not-yet-grabbed downloads (per-user, file still on disk).

    Lets someone who closed the tab by mistake recover their file before the
    TTL sweep deletes it. Each entry carries when it expires.
    """
    ready_dir = Path(settings.DATA_DIR) / "ready"
    ttl = timedelta(minutes=settings.FILE_TTL_MINUTES)
    snapshot = await job_svc.all_jobs_snapshot()

    out = []
    for job_id, job in snapshot.items():
        if job.status != "clean" or job.user_id != user["id"]:
            continue
        if not job.ext or not (ready_dir / f"{job_id}.{job.ext}").exists():
            continue
        expires_at = None
        if job.ready_at:
            try:
                expires_at = (datetime.fromisoformat(job.ready_at) + ttl).isoformat()
            except ValueError:
                pass
        out.append({
            "job_id": job_id,
            "title": job.title,
            "ext": job.ext,
            "source": job.source,
            "download_url": f"/api/files/{job_id}",
            "ready_at": job.ready_at,
            "expires_at": expires_at,
        })

    out.sort(key=lambda d: d.get("ready_at") or "", reverse=True)
    return out


@router.get("/history")
async def history(user: dict = Depends(require_user)):
    async with get_db() as db:
        cur = await db.execute(
            "SELECT id, title, author, source, ext, sha256, verdict, created_at"
            " FROM history WHERE user_id = ? ORDER BY created_at DESC LIMIT 50",
            (user["id"],),
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


@router.delete("/history/{entry_id}")
async def delete_history_entry(entry_id: int, user: dict = Depends(require_user)):
    """Delete one of the user's own history rows."""
    async with write_db() as db:
        cur = await db.execute(
            "DELETE FROM history WHERE id = ? AND user_id = ?",
            (entry_id, user["id"]),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="History entry not found")
    return {"deleted": entry_id}


@router.delete("/history")
async def clear_history(user: dict = Depends(require_user)):
    """Clear the user's entire history (their rows only)."""
    async with write_db() as db:
        cur = await db.execute(
            "DELETE FROM history WHERE user_id = ?", (user["id"],)
        )
    return {"deleted": cur.rowcount}
