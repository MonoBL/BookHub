"""Job routes: POST /api/download, GET /api/jobs/{id}, GET /api/history,
GET /api/downloads. See BUILD.md §7.1."""
import asyncio
import difflib
import logging
import unicodedata
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
from app.providers.base import DownloadPlan, SearchResult
from app.services import jobs as job_svc
from app.services.downloader import download_with_fallback
from app.services.scanner import verify_and_scan

router = APIRouter()

logger = logging.getLogger("bookhub")

# Cross-provider fallback. Every Libgen mirror hands out a get.php link that
# redirects to the same CDN host for a given md5, so one dead host takes the
# whole candidate list down at once and no amount of in-provider retrying
# helps. Rather than surface a bare failure, look the same book up on a
# different provider. archive.org is the only safe target today: public,
# no Cloudflare gate, stable URLs.
_FALLBACK_SOURCES = ("archive",)
# Near-exact title match only. A loose match would silently hand the user a
# different book, which is worse than a clear failure.
_FALLBACK_TITLE_MIN_RATIO = 0.85
# How many of the best-matching hits to try resolving before giving up.
_FALLBACK_MAX_CANDIDATES = 3

_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class DownloadRequest(BaseModel):
    result: dict  # SearchResult as dict
    force_rescan: bool = False  # user-initiated re-scan: force fresh VT analysis


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

    asyncio.create_task(_run_pipeline(job_id, result, provider, ext, body.force_rescan))
    return {"job_id": job_id}


async def _resolve_plans(provider, result: SearchResult) -> list[DownloadPlan]:
    # Providers that can yield multiple CDN hosts (Libgen) let the downloader
    # fall back when one host 503s our server IP.
    if hasattr(provider, "resolve_candidates"):
        return await provider.resolve_candidates(result)
    return [await provider.resolve(result)]


async def _external_if_oversized(job_id: str, source: str, plans: list[DownloadPlan]) -> bool:
    """Hand back the direct link for an oversized public file. True if handled.

    Oversized public files (e.g. archive.org comics/scans) would only hit the
    download size cap. Rather than fail, hand the user the direct link so they
    fetch it straight from the source. Only for sources whose URLs are stable,
    public and safe to share (archive.org); Libgen/AA links are one-shot,
    gated, or behind Cloudflare and must keep going through us.
    """
    if source != "archive" or not plans:
        return False
    plan = plans[0]
    cap = settings.DOWNLOAD_MAX_MB * 1024 * 1024
    if not plan.size_bytes or plan.size_bytes <= cap:
        return False
    await job_svc.update_job(
        job_id,
        status="external",
        download_url=plan.url,
        reason=f"{plan.size_bytes // (1024 * 1024)} MB — too large to process here",
    )
    return True


def _normalize_title(value: str) -> str:
    """Accent-stripped, punctuation-free lowercase form for title comparison."""
    decomposed = unicodedata.normalize("NFKD", value or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", stripped.lower()).strip()


async def _fallback_plans(
    result: SearchResult, ext: str
) -> tuple[list[DownloadPlan], str] | None:
    """Locate the same book on another provider. Returns (plans, source) or None."""
    wanted = _normalize_title(result.title)
    if not wanted:
        return None

    for name in _FALLBACK_SOURCES:
        if name == result.source:
            continue
        provider = next((p for p in PROVIDERS if p.name == name and p.enabled), None)
        if not provider:
            continue

        try:
            hits = await provider.search(result.title, [ext])
        except Exception as exc:
            logger.info("fallback_search_failed source=%s detail=%s", name, exc)
            continue

        scored = [
            (difflib.SequenceMatcher(None, wanted, _normalize_title(h.title)).ratio(), h)
            for h in hits
            if h.ext == ext
        ]
        scored = [pair for pair in scored if pair[0] >= _FALLBACK_TITLE_MIN_RATIO]
        scored.sort(key=lambda pair: pair[0], reverse=True)

        for _, hit in scored[:_FALLBACK_MAX_CANDIDATES]:
            try:
                plans = await _resolve_plans(provider, hit)
            except Exception as exc:
                logger.info("fallback_resolve_failed source=%s detail=%s", name, exc)
                continue
            if plans:
                return plans, name

    return None


def _client_reason(exc: Exception) -> str:
    """Client-safe failure reason. Never leaks mirror hosts, paths or keys."""
    text = str(exc).lower()
    if "timeout" in text or "timed out" in text:
        return "The source did not respond. Try another result or retry later."
    if any(code in text for code in ("500", "502", "503", "504")) or "server error" in text:
        return "The source mirrors are down. Try another result or retry later."
    if "no download candidates" in text or "resolve failed" in text:
        return "No working download link for this result."
    return "Download failed"


async def _run_pipeline(
    job_id: str, result: SearchResult, provider, ext: str, force_rescan: bool = False
) -> None:
    try:
        plans = await _resolve_plans(provider, result)
        if await _external_if_oversized(job_id, result.source, plans):
            return

        try:
            path = await download_with_fallback(job_id, plans, ext)
        except Exception as exc:
            # Two distinct failures land here and both are worth a second
            # source: a dead CDN, and a file over the size cap (archive.org
            # often carries a small reflowable edition of the same title next
            # to the giant scan, and resolve_candidates puts it first).
            before = await job_svc.get_job(job_id)
            was_blocked = bool(before and before.status == "blocked")
            block_reason = before.reason if was_blocked else None

            fallback = await _fallback_plans(result, ext)
            if not fallback:
                raise
            alt_plans, alt_source = fallback
            logger.info(
                "cross_provider_fallback job=%s from=%s to=%s after=%s",
                job_id, result.source, alt_source, exc,
            )
            if await _external_if_oversized(job_id, alt_source, alt_plans):
                return
            await job_svc.update_job(job_id, source=alt_source)
            try:
                path = await download_with_fallback(job_id, alt_plans, ext)
            except Exception:
                if was_blocked:
                    # The substitute did not work out either, so put the
                    # original verdict back: why the first file was rejected is
                    # more useful than a vaguer transport error.
                    await job_svc.update_job(
                        job_id, status="blocked", reason=block_reason, source=result.source,
                    )
                raise

        await verify_and_scan(job_id, path, ext, force_rescan=force_rescan)
    except Exception as exc:
        current = await job_svc.get_job(job_id)
        if current and current.status not in ("blocked", "unverified", "clean", "external"):
            # Log the real error server-side; show the client a sanitised reason
            # so internal details (mirror hosts, paths) are never leaked.
            logger.warning("download_pipeline_error job=%s detail=%s", job_id, exc)
            await job_svc.update_job(job_id, status="error", reason=_client_reason(exc))


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

    # Never expose the owner's internal user_id to the client.
    return job.model_dump(exclude={"user_id"})


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
