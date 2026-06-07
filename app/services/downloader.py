"""Download service: fetch -> quarantine with stall guard and size cap. See BUILD.md §7.2."""
import asyncio
import logging
from pathlib import Path

import httpx

from app.config import settings
from app.providers.base import DownloadPlan
from app.services import jobs as job_svc

logger = logging.getLogger("bookhub")

# Shared semaphore enforces DOWNLOAD_CONCURRENCY.
_dl_sem: asyncio.Semaphore | None = None


def _get_sem() -> asyncio.Semaphore:
    global _dl_sem
    if _dl_sem is None:
        _dl_sem = asyncio.Semaphore(settings.DOWNLOAD_CONCURRENCY)
    return _dl_sem


async def download(job_id: str, plan: DownloadPlan, ext: str) -> Path:
    """
    Download a file into quarantine. Returns path on success.
    Updates job status on failure and raises RuntimeError.
    """
    quarantine_dir = Path(settings.DATA_DIR) / "quarantine"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    dest = quarantine_dir / f"{job_id}.{ext}"

    max_bytes = settings.DOWNLOAD_MAX_MB * 1024 * 1024
    # Absolute time ceiling: ~25 minutes so a trickle can't hold the semaphore forever.
    abs_timeout = 1500

    sem = _get_sem()
    await job_svc.update_job(job_id, status="queued")

    async with sem:
        await job_svc.update_job(job_id, status="downloading")
        try:
            await asyncio.wait_for(
                _stream(job_id, plan, dest, max_bytes),
                timeout=abs_timeout,
            )
        except asyncio.TimeoutError:
            dest.unlink(missing_ok=True)
            await job_svc.update_job(job_id, status="error", reason="download timed out")
            raise RuntimeError(f"Download timed out for job {job_id}")
        except RuntimeError:
            raise
        except Exception as exc:
            dest.unlink(missing_ok=True)
            await job_svc.update_job(job_id, status="error", reason=str(exc))
            raise RuntimeError(str(exc))

    return dest


# Libgen mirrors all redirect get.php to the same CDN (e.g. cdn3.booksdl.lc),
# which intermittently 503s a datacenter IP: empirically ~1 attempt in 8 wins.
# So a single pass over the "distinct" candidates almost always fails. Retry
# each candidate a few times with backoff before giving up.
_DOWNLOAD_ATTEMPTS_PER_PLAN = 5
_DOWNLOAD_RETRY_BACKOFF_S = 3.0


async def download_with_fallback(
    job_id: str, plans: list[DownloadPlan], ext: str
) -> Path:
    """Try each candidate download URL, retrying transient failures.

    Libgen hands out get.php links that redirect to a CDN host (e.g.
    cdn3.booksdl.lc) which sometimes 503s a server's datacenter IP while serving
    residential IPs fine. The mirrors look distinct but funnel to the same CDN,
    so falling back across candidates is not enough: we also retry each
    candidate with backoff to ride out the intermittent 503s. A size-cap
    'blocked' is final and stops the loop (retrying would hit the same oversized
    file).
    """
    if not plans:
        raise RuntimeError("no download candidates")

    last_exc: Exception | None = None
    for i, plan in enumerate(plans, start=1):
        for attempt in range(1, _DOWNLOAD_ATTEMPTS_PER_PLAN + 1):
            try:
                return await download(job_id, plan, ext)
            except RuntimeError as exc:
                last_exc = exc
                current = await job_svc.get_job(job_id)
                if current and current.status == "blocked":
                    raise
                logger.warning(
                    "download candidate %d/%d attempt %d/%d failed job=%s: %s",
                    i, len(plans), attempt, _DOWNLOAD_ATTEMPTS_PER_PLAN, job_id, exc,
                )
                if attempt < _DOWNLOAD_ATTEMPTS_PER_PLAN:
                    await asyncio.sleep(_DOWNLOAD_RETRY_BACKOFF_S * attempt)

    raise last_exc or RuntimeError("all download candidates failed")


async def _stream(job_id: str, plan: DownloadPlan, dest: Path, max_bytes: int) -> None:
    timeout = httpx.Timeout(connect=60.0, read=30.0, write=30.0, pool=5.0)

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout,
        headers=plan.headers,
        cookies=plan.cookies,
    ) as client:
        async with client.stream("GET", plan.url) as r:
            r.raise_for_status()

            # Reject before first byte if Content-Length exceeds cap.
            cl = r.headers.get("content-length")
            if cl and int(cl) > max_bytes:
                await job_svc.update_job(
                    job_id, status="blocked", reason="exceeds size cap"
                )
                raise RuntimeError("exceeds size cap (content-length)")

            received = 0
            with dest.open("wb") as fh:
                async for chunk in r.aiter_bytes(chunk_size=65536):
                    received += len(chunk)
                    if received > max_bytes:
                        fh.close()
                        dest.unlink(missing_ok=True)
                        await job_svc.update_job(
                            job_id, status="blocked", reason="exceeds size cap"
                        )
                        raise RuntimeError("exceeds size cap (streaming)")
                    fh.write(chunk)
