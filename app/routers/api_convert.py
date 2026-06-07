"""Convert routes: POST /api/convert, POST /api/convert-bmp. See BUILD.md §8.1."""
import asyncio
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form

from app.auth import require_user
from app.config import settings
from app.db import write_db
from app.models import Job
from app.services import jobs as job_svc
from app.services.bmp import convert_images_to_bmp
from app.services.converter import convert_pdf
from app.services.scanner import verify_format

router = APIRouter()

# Magic-byte sniff for accepted image inputs (defence before Pillow decodes).
_IMAGE_MAGIC = {
    b"\xff\xd8\xff": "jpg",
    b"\x89PNG\r\n\x1a\n": "png",
    b"BM": "bmp",
    b"II*\x00": "tiff",
    b"MM\x00*": "tiff",
}


def _sniff_image(data: bytes) -> str | None:
    for magic, kind in _IMAGE_MAGIC.items():
        if data.startswith(magic):
            return kind
    # WEBP: "RIFF....WEBP"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


async def _record_clean(job_id: str, ext: str) -> None:
    """Mark a converter job clean, stamp ready_at, and log to the user's history."""
    now = datetime.now(timezone.utc).isoformat()
    await job_svc.update_job(
        job_id,
        status="clean",
        ext=ext,
        download_url=f"/api/files/{job_id}",
        ready_at=now,
    )
    job = await job_svc.get_job(job_id)
    if job and job.user_id:
        async with write_db() as db:
            await db.execute(
                "INSERT INTO history (user_id, title, author, source, ext, sha256, verdict, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (job.user_id, job.title, job.author, "convert", ext, None, "clean", now),
            )

_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@router.post("/convert")
async def start_convert(
    file: UploadFile = File(...),
    title: str = Form(..., min_length=1, max_length=500),
    author: str = Form(default="Unknown Author", max_length=500),
    comic: bool = Form(default=False),
    user: dict = Depends(require_user),
):
    max_bytes = settings.CONVERT_MAX_MB * 1024 * 1024

    # Read up to max_bytes + 1 to detect over-limit.
    chunk = await file.read(max_bytes + 1)
    if len(chunk) > max_bytes:
        raise HTTPException(status_code=413, detail=f"PDF exceeds {settings.CONVERT_MAX_MB} MB limit")

    data = chunk

    # Format check: PDF magic + polyglot guard (same as scanner §7.3).
    if not data[:5] == b"%PDF-":
        raise HTTPException(status_code=400, detail="File must be a PDF")

    if b"PK\x05\x06" in data[-65536:]:
        raise HTTPException(status_code=400, detail="Polyglot PDF/ZIP rejected")

    job_id = str(uuid.uuid4())
    job = Job(id=job_id, status="queued", ext="epub", title=title, author=author,
              user_id=user["id"], source="convert")
    await job_svc.create_job(job)

    # Write PDF to scratch dir.
    scratch_dir = Path(settings.DATA_DIR) / "jobs" / job_id
    scratch_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = scratch_dir / "in.pdf"
    pdf_path.write_bytes(data)

    asyncio.create_task(_run_convert_pipeline(job_id, pdf_path, title, author, comic))
    return {"job_id": job_id}


async def _run_convert_pipeline(
    job_id: str, pdf_path: Path, title: str, author: str, comic: bool = False
) -> None:
    scratch_dir = pdf_path.parent
    try:
        await convert_pdf(job_id, pdf_path, title, author, comic=comic)
        # No VT scan - user's own file. Move to ready/ and mark clean.
        await _record_clean(job_id, "epub")
    except Exception as exc:
        await job_svc.update_job(job_id, status="error", reason=str(exc)[:500])
    finally:
        shutil.rmtree(str(scratch_dir), ignore_errors=True)


@router.post("/convert-bmp")
async def start_convert_bmp(
    files: list[UploadFile] = File(...),
    width: int = Form(default=settings.BMP_DEFAULT_WIDTH, ge=16, le=4096),
    height: int = Form(default=settings.BMP_DEFAULT_HEIGHT, ge=16, le=4096),
    grayscale: bool = Form(default=True),
    user: dict = Depends(require_user),
):
    if not files:
        raise HTTPException(status_code=400, detail="No images uploaded")
    if len(files) > settings.BMP_MAX_FILES:
        raise HTTPException(
            status_code=400, detail=f"Too many images (max {settings.BMP_MAX_FILES})"
        )

    max_bytes = settings.IMAGE_MAX_MB * 1024 * 1024
    total = 0
    images: list[tuple[str, bytes]] = []
    for f in files:
        data = await f.read(max_bytes + 1)
        total += len(data)
        if total > max_bytes:
            raise HTTPException(
                status_code=413, detail=f"Images exceed {settings.IMAGE_MAX_MB} MB total"
            )
        if _sniff_image(data) is None:
            raise HTTPException(
                status_code=400,
                detail=f"'{f.filename}' is not a supported image (JPG, PNG, WEBP, BMP, TIFF)",
            )
        images.append((f.filename or "image", data))

    title = f"{len(images)} image{'s' if len(images) != 1 else ''} to BMP"
    job_id = str(uuid.uuid4())
    job = Job(id=job_id, status="queued", ext="bmp", title=title,
              user_id=user["id"], source="convert")
    await job_svc.create_job(job)

    asyncio.create_task(_run_bmp_pipeline(job_id, images, width, height, grayscale))
    return {"job_id": job_id}


async def _run_bmp_pipeline(
    job_id: str, images: list[tuple[str, bytes]], width: int, height: int, grayscale: bool
) -> None:
    ready_dir = Path(settings.DATA_DIR) / "ready"
    scratch_dir = ready_dir / f"_bmp_{job_id}"
    try:
        await job_svc.update_job(job_id, status="converting")
        ext = await asyncio.get_event_loop().run_in_executor(
            None, convert_images_to_bmp, images, scratch_dir, width, height, grayscale
        )
        # Move produced file to ready/<job_id>.<ext> (served + TTL-swept there).
        src = scratch_dir / f"out.{ext}"
        dest = ready_dir / f"{job_id}.{ext}"
        src.replace(dest)
        await _record_clean(job_id, ext)
    except Exception as exc:
        await job_svc.update_job(job_id, status="error", reason=str(exc)[:500])
    finally:
        shutil.rmtree(str(scratch_dir), ignore_errors=True)
