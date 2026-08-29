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
                _stream(job_id, plan, dest, max_bytes, ext),
                timeout=abs_timeout,
            )
        # A failed attempt leaves the job alone: it is one candidate out of
        # several, the retry loop is still working, and an httpx error names the
        # mirror host and the one-shot key. Writing that into the job would put
        # it straight on the user's screen while the retry loop is still going.
        # The raw detail goes to the log; the pipeline sanitises what the client
        # sees once every candidate is spent.
        except asyncio.TimeoutError:
            dest.unlink(missing_ok=True)
            raise RuntimeError("download timed out")
        except RuntimeError:
            raise
        except Exception as exc:
            dest.unlink(missing_ok=True)
            # httpx timeout exceptions stringify to '', which produced blank
            # failure reasons in the logs and left the caller nothing to
            # classify. Fall back to the class name (e.g. "ReadTimeout").
            detail = str(exc) or type(exc).__name__
            logger.warning("download_attempt_failed job=%s detail=%s", job_id, detail)
            raise RuntimeError(detail)

    return dest


# Libgen mirrors all redirect get.php to the same CDN (e.g. cdn3.booksdl.lc),
# which intermittently 503s a datacenter IP: empirically only ~1 attempt in
# several wins, and the bad window can last a while. A single pass over the
# candidates almost always fails. Instead we round-robin the candidates and
# keep retrying, with a short delay between rounds, until a total wall-clock
# budget is spent. p(success) compounds quickly across ~20+ tries.
_DOWNLOAD_RETRY_BUDGET_S = 120.0  # total time to keep retrying transient failures
_DOWNLOAD_RETRY_DELAY_S = 3.0     # pause between full round-robin rounds


async def download_with_fallback(
    job_id: str, plans: list[DownloadPlan], ext: str
) -> Path:
    """Try each candidate download URL, retrying transient failures.

    Libgen hands out get.php links that redirect to a CDN host (e.g.
    cdn3.booksdl.lc) which frequently 503s a server's datacenter IP while
    serving residential IPs fine. The mirrors look distinct but funnel to the
    same CDN, so falling back across candidates once is not enough: we
    round-robin every candidate and keep retrying within a time budget to ride
    out the 503 window. A size-cap 'blocked' is final and stops the loop
    (retrying would hit the same oversized file).
    """
    if not plans:
        raise RuntimeError("no download candidates")

    loop = asyncio.get_event_loop()
    deadline = loop.time() + _DOWNLOAD_RETRY_BUDGET_S
    last_exc: Exception | None = None
    attempt = 0

    while True:
        for i, plan in enumerate(plans, start=1):
            attempt += 1
            try:
                return await download(job_id, plan, ext)
            except RuntimeError as exc:
                last_exc = exc
                current = await job_svc.get_job(job_id)
                if current and current.status == "blocked":
                    raise
                logger.warning(
                    "download attempt %d (candidate %d/%d) failed job=%s: %s",
                    attempt, i, len(plans), job_id, exc,
                )

        if loop.time() >= deadline:
            break
        # Keep the job marked as downloading while we wait out the bad window.
        await job_svc.update_job(job_id, status="downloading")
        await asyncio.sleep(_DOWNLOAD_RETRY_DELAY_S)

    raise last_exc or RuntimeError("all download candidates failed")


# A dead Libgen CDN node answers get.php with HTTP 200 and the nginx default
# welcome page instead of the file. Written to quarantine it reached the
# scanner as a 638-byte "epub" and failed format verification, which is final:
# the job ended as "blocked: not a real ZIP" and the retry loop never got to
# try another candidate. Catching it here makes it an ordinary transient
# failure, so the round-robin rides out the bad node like any other 503.
_MAGIC = {
    "epub": b"PK\x03\x04",
    "pdf": b"%PDF-",
}


def _reject_non_file(ext: str, head: bytes) -> str | None:
    """Return a failure reason if `head` is not the start of an `ext` file."""
    magic = _MAGIC.get(ext)
    if magic and not head.startswith(magic):
        return f"source returned a non-{ext} payload"
    return None


async def _stream(
    job_id: str, plan: DownloadPlan, dest: Path, max_bytes: int, ext: str
) -> None:
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

            ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype in ("text/html", "application/xhtml+xml"):
                raise RuntimeError("source returned a web page, not a file")

            received = 0
            head = b""
            with dest.open("wb") as fh:
                async for chunk in r.aiter_bytes(chunk_size=65536):
                    received += len(chunk)
                    if len(head) < 8:
                        head += chunk[: 8 - len(head)]
                        if len(head) >= 8:
                            reason = _reject_non_file(ext, head)
                            if reason:
                                fh.close()
                                dest.unlink(missing_ok=True)
                                raise RuntimeError(reason)
                    if received > max_bytes:
                        fh.close()
                        dest.unlink(missing_ok=True)
                        await job_svc.update_job(
                            job_id, status="blocked", reason="exceeds size cap"
                        )
                        raise RuntimeError("exceeds size cap (streaming)")
                    fh.write(chunk)

            # A file shorter than the sniff window never hit the check above.
            reason = _reject_non_file(ext, head)
            if reason:
                dest.unlink(missing_ok=True)
                raise RuntimeError(reason)
