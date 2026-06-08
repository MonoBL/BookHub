"""File serve + delete routes. See BUILD.md §7.6."""
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response

from app.auth import require_user
from app.config import settings
from app.services import jobs as job_svc

router = APIRouter()

# Cover proxy limits.
_COVER_MAX_BYTES = 2 * 1024 * 1024  # 2 MB is plenty for a thumbnail
_COVER_ALLOWED_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp")
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _cover_host_allowed(host: str) -> bool:
    """SSRF guard: only fetch covers from known provider hosts (exact or subdomain)."""
    host = host.lower()
    allowed = {m.strip().lower() for m in settings.LIBGEN_MIRRORS.split(",") if m.strip()}
    allowed.add("annas-archive.org")
    allowed.add("archive.org")
    return any(host == a or host.endswith("." + a) for a in allowed)

_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_CONTENT_TYPES = {
    "epub": "application/epub+zip",
    "pdf": "application/pdf",
    "bmp": "image/bmp",
    "zip": "application/zip",
}


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

    # IDOR: non-admin users can only access their own jobs.
    if job.user_id is not None and job.user_id != user["id"] and not user.get("is_admin"):
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status != "clean":
        raise HTTPException(status_code=404, detail="File not available")

    if await job_svc.is_in_flight(job_id):
        raise HTTPException(status_code=404, detail="File not available")

    ext = job.ext
    if ext not in _CONTENT_TYPES:
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


@router.get("/cover")
async def cover(u: str = Query(..., max_length=2048)):
    """Proxy a provider cover thumbnail so the browser stays same-origin (CSP).

    Locked down against SSRF: https only, host allowlisted to provider domains,
    no redirect following, size-capped, image content-types only.
    """
    parsed = urlsplit(u)
    if parsed.scheme != "https" or not parsed.hostname:
        raise HTTPException(status_code=400, detail="Invalid cover URL")
    if not _cover_host_allowed(parsed.hostname):
        raise HTTPException(status_code=400, detail="Cover host not allowed")

    try:
        async with httpx.AsyncClient(
            follow_redirects=False,  # a redirect could escape the allowlist
            timeout=httpx.Timeout(connect=10.0, read=10.0, write=10.0, pool=5.0),
            headers={"User-Agent": _BROWSER_UA},
        ) as client:
            r = await client.get(u)
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Cover fetch failed")

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail="Cover unavailable")

    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype not in _COVER_ALLOWED_TYPES:
        raise HTTPException(status_code=415, detail="Not an image")

    body = r.content
    if len(body) > _COVER_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Cover too large")

    return Response(
        content=body,
        media_type=ctype,
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def _cleanup(job_id: str, file_path: Path) -> None:
    """Best-effort delete after send; remove from in-flight set."""
    try:
        file_path.unlink(missing_ok=True)
    except Exception:
        pass
    await job_svc.remove_in_flight(job_id)
