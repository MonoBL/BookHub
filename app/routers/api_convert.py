"""Convert routes: POST /api/convert. See BUILD.md §8.1."""
import asyncio
import re
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form

from app.auth import require_user
from app.config import settings
from app.models import Job
from app.services import jobs as job_svc
from app.services.converter import convert_pdf
from app.services.scanner import verify_format

router = APIRouter()

_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@router.post("/convert")
async def start_convert(
    file: UploadFile = File(...),
    title: str = Form(..., min_length=1, max_length=500),
    author: str = Form(default="Unknown Author", max_length=500),
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
    job = Job(id=job_id, status="queued", ext="epub", title=title)
    await job_svc.create_job(job)

    # Write PDF to scratch dir.
    scratch_dir = Path(settings.DATA_DIR) / "jobs" / job_id
    scratch_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = scratch_dir / "in.pdf"
    pdf_path.write_bytes(data)

    asyncio.create_task(_run_convert_pipeline(job_id, pdf_path, title, author))
    return {"job_id": job_id}


async def _run_convert_pipeline(
    job_id: str, pdf_path: Path, title: str, author: str
) -> None:
    scratch_dir = pdf_path.parent
    try:
        out_epub = await convert_pdf(job_id, pdf_path, title, author)
        # No VT scan - user's own file. Move to ready/ and mark clean.
        await job_svc.update_job(
            job_id,
            status="clean",
            download_url=f"/api/files/{job_id}",
        )
    except Exception as exc:
        await job_svc.update_job(job_id, status="error", reason=str(exc)[:500])
    finally:
        shutil.rmtree(str(scratch_dir), ignore_errors=True)
