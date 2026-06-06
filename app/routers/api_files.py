"""File serve + delete routes. See BUILD.md §7.6."""
import re
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse

from app.auth import require_user
from app.config import settings
from app.services import jobs as job_svc

router = APIRouter()

_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_CONTENT_TYPES = {"epub": "application/epub+zip", "pdf": "application/pdf"}


def _safe_filename(title: str | None, ext: str) -> str:
    """Sanitize title into a safe filename."""
    base = title or "download"
    safe = re.sub(r"[^A-Za-z0-9 ._-]", "", base)[:120].strip() or "download"
    return f"{safe}.{ext}"


@router.get("/files/{job_id}")
async def serve_file(
    job_id: str,
    background_tasks: BackgroundTasks,
    user: dict = Depends(require_user),
):
    if not _UUID4_RE.match(job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID format")

    job = await job_svc.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status not in ("clean", "consumed"):
        raise HTTPException(status_code=404, detail="File not available")

    ext = job.ext
    if ext not in ("epub", "pdf"):
        raise HTTPException(status_code=400, detail="Invalid file type in job")

    ready_dir = Path(settings.DATA_DIR) / "ready"
    file_path = (ready_dir / f"{job_id}.{ext}").resolve()

    # Path traversal guard: resolved path must be inside ready_dir.
    try:
        file_path.relative_to(ready_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Path traversal rejected")

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File no longer available")

    await job_svc.add_in_flight(job_id)
    await job_svc.update_job(job_id, status="consumed")

    filename = _safe_filename(job.title, ext)
    media_type = _CONTENT_TYPES.get(ext, "application/octet-stream")

    background_tasks.add_task(_cleanup, job_id, file_path)

    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type=media_type,
    )


async def _cleanup(job_id: str, file_path: Path) -> None:
    """Best-effort delete after send; remove from in-flight set."""
    try:
        file_path.unlink(missing_ok=True)
    except Exception:
        pass
    await job_svc.remove_in_flight(job_id)
