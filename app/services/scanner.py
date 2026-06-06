"""
Format verify + VirusTotal scanner. See BUILD.md §7.3, §7.4, §7.5.

Policy: files are served ONLY with a fresh "clean" VT verdict. Everything else
is not served (malicious/suspicious deleted; unverified deleted, not served).
"""
import asyncio
import hashlib
import json
import logging
import re
import time
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal

import httpx

from app.config import settings
from app.db import get_db, write_db
from app.services import jobs as job_svc

logger = logging.getLogger("bookhub")

# VirusTotal free tier: 4 requests/minute, 500/day, 32 MB upload cap.
_VT_RATE_LIMIT = 4  # requests per minute
_VT_UPLOAD_CAP_BYTES = 32 * 1024 * 1024
_VT_BASE = "https://www.virustotal.com/api/v3"

# Scan concurrency semaphore.
_scan_sem: asyncio.Semaphore | None = None

# Token bucket for VT rate limiting (4 req/min).
_vt_lock = asyncio.Lock()
_vt_tokens: float = 4.0
_vt_last_refill: float = 0.0

# Daily counter (reset at UTC midnight).
_vt_daily_lock = asyncio.Lock()
_vt_daily_count: int = 0
_vt_daily_reset_date: str = ""

Verdict = Literal["clean", "malicious", "suspicious", "unverified"]


def _get_scan_sem() -> asyncio.Semaphore:
    global _scan_sem
    if _scan_sem is None:
        _scan_sem = asyncio.Semaphore(settings.SCAN_CONCURRENCY)
    return _scan_sem


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Format verification
# ---------------------------------------------------------------------------

def _check_active_content(zf: zipfile.ZipFile) -> bool:
    """Return True if EPUB contains active content (JS, remote resources, etc.)."""
    for name in zf.namelist():
        lname = name.lower()
        if lname.endswith(".js"):
            return True
        if lname.endswith((".xhtml", ".html", ".htm", ".svg", ".xml")):
            try:
                data = zf.read(name)
                text = data.decode("utf-8", errors="replace")
                if "<script" in text or "http://" in text or "https://" in text:
                    return True
            except Exception:
                pass
    return False


def verify_format(path: Path, ext: str) -> dict:
    """
    Magic-byte and structure check. Returns {"ok": True} or {"ok": False, "reason": ...}.
    Also returns "has_active_content" for EPUBs (§7.5).
    """
    data = path.read_bytes()

    if ext == "pdf":
        if not data[:5] == b"%PDF-":
            return {"ok": False, "reason": "not a real PDF (bad magic bytes)"}
        # Polyglot guard: reject if tail contains ZIP EOCD (PK\x05\x06).
        if b"PK\x05\x06" in data[-65536:]:
            return {"ok": False, "reason": "PDF/ZIP polyglot rejected"}
        return {"ok": True}

    if ext == "epub":
        try:
            zf = zipfile.ZipFile(path, "r")
        except zipfile.BadZipFile:
            return {"ok": False, "reason": "not a real ZIP (EPUB must be a ZIP)"}

        with zf:
            infolist = zf.infolist()
            if len(infolist) > 2000:
                return {"ok": False, "reason": "entry-flood: too many ZIP entries"}

            total_uncompressed = sum(zi.file_size for zi in infolist)
            on_disk = path.stat().st_size
            if total_uncompressed > 1_500_000_000:
                return {"ok": False, "reason": "zip-bomb: declared size too large"}
            if on_disk > 0 and total_uncompressed / on_disk > 100:
                return {"ok": False, "reason": "zip-bomb: suspicious compression ratio"}

            names = zf.namelist()
            if "mimetype" not in names:
                return {"ok": False, "reason": "not a real EPUB (missing mimetype entry)"}

            mime = zf.read("mimetype")
            if mime.strip() != b"application/epub+zip":
                return {"ok": False, "reason": "not a real EPUB (wrong mimetype)"}

            has_active = _check_active_content(zf)

        return {"ok": True, "has_active_content": has_active}

    return {"ok": False, "reason": f"unsupported extension: {ext}"}


# ---------------------------------------------------------------------------
# VirusTotal rate limiting
# ---------------------------------------------------------------------------

async def _vt_acquire() -> None:
    """Wait until a VT rate-limit token is available (4/min token bucket)."""
    global _vt_tokens, _vt_last_refill
    async with _vt_lock:
        now = time.monotonic()
        if _vt_last_refill == 0.0:
            _vt_last_refill = now
        elapsed = now - _vt_last_refill
        refill = elapsed * (_VT_RATE_LIMIT / 60.0)
        _vt_tokens = min(float(_VT_RATE_LIMIT), _vt_tokens + refill)
        _vt_last_refill = now

        if _vt_tokens < 1.0:
            wait = (1.0 - _vt_tokens) / (_VT_RATE_LIMIT / 60.0)
            await asyncio.sleep(wait)
            _vt_tokens = 0.0
        else:
            _vt_tokens -= 1.0


async def _vt_daily_check() -> bool:
    """Return True if daily quota is available; increment counter."""
    global _vt_daily_count, _vt_daily_reset_date
    async with _vt_daily_lock:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != _vt_daily_reset_date:
            _vt_daily_count = 0
            _vt_daily_reset_date = today
        if _vt_daily_count >= settings.VT_DAILY_CAP:
            return False
        _vt_daily_count += 1
        return True


async def vt_daily_remaining() -> int:
    async with _vt_daily_lock:
        return max(0, settings.VT_DAILY_CAP - _vt_daily_count)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

async def _cache_lookup(sha256: str) -> dict | None:
    """Return a fresh cache row or None (stale/missing = miss)."""
    async with get_db() as db:
        cur = await db.execute(
            "SELECT verdict, malicious_count, suspicious_count, engines_total,"
            " last_analysis_date, checked_at FROM vt_cache WHERE sha256 = ?",
            (sha256,),
        )
        row = await cur.fetchone()
    if not row:
        return None

    row = dict(row)
    if not row["last_analysis_date"]:
        return None

    # Freshness check: last_analysis_date within VT_CLEAN_MAX_AGE_DAYS AND engines >= min.
    try:
        lad = datetime.fromisoformat(row["last_analysis_date"])
        age_limit = timedelta(days=settings.VT_CLEAN_MAX_AGE_DAYS)
        if datetime.now(timezone.utc) - lad > age_limit:
            return None  # stale
    except Exception:
        return None

    if (row["engines_total"] or 0) < settings.VT_MIN_ENGINES:
        return None  # too few engines

    return row


async def _cache_store(sha256: str, verdict: str, stats: dict, lad: str | None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with write_db() as db:
        await db.execute(
            """
            INSERT INTO vt_cache (sha256, verdict, malicious_count, suspicious_count,
                engines_total, last_analysis_date, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sha256) DO UPDATE SET
                verdict = excluded.verdict,
                malicious_count = excluded.malicious_count,
                suspicious_count = excluded.suspicious_count,
                engines_total = excluded.engines_total,
                last_analysis_date = excluded.last_analysis_date,
                checked_at = excluded.checked_at
            """,
            (
                sha256,
                verdict,
                stats.get("malicious", 0),
                stats.get("suspicious", 0),
                stats.get("total", 0),
                lad,
                now,
            ),
        )


# ---------------------------------------------------------------------------
# VT API calls
# ---------------------------------------------------------------------------

def _map_stats(stats: dict, lad: str | None, engines_total: int) -> Verdict:
    """Map last_analysis_stats to a verdict, applying freshness + engine floor."""
    if stats.get("malicious", 0) >= 1:
        return "malicious"
    if stats.get("suspicious", 0) >= 2:
        return "suspicious"
    # Clean only if fresh and enough engines.
    if not lad:
        return "unverified"
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(lad)
        if age > timedelta(days=settings.VT_CLEAN_MAX_AGE_DAYS):
            return "unverified"
    except Exception:
        return "unverified"
    if engines_total < settings.VT_MIN_ENGINES:
        return "unverified"
    return "clean"


async def _vt_lookup(sha256: str, client: httpx.AsyncClient) -> tuple[Verdict, dict, str | None]:
    """GET /api/v3/files/{sha256}. Returns (verdict, stats, last_analysis_date)."""
    if not await _vt_daily_check():
        return "unverified", {}, None
    await _vt_acquire()

    try:
        r = await client.get(
            f"{_VT_BASE}/files/{sha256}",
            headers={"x-apikey": settings.VT_API_KEY},
            timeout=30.0,
        )
    except Exception as exc:
        logger.warning(json.dumps({"kind": "vt_error", "detail": str(exc)}))
        return "unverified", {}, None

    if r.status_code == 429:
        await asyncio.sleep(60)
        # Retry once.
        if not await _vt_daily_check():
            return "unverified", {}, None
        await _vt_acquire()
        r = await client.get(
            f"{_VT_BASE}/files/{sha256}",
            headers={"x-apikey": settings.VT_API_KEY},
            timeout=30.0,
        )
        if r.status_code == 429:
            return "unverified", {}, None

    if r.status_code == 404:
        return "not_found", {}, None  # type: ignore[return-value]

    if r.status_code != 200:
        return "unverified", {}, None

    body = r.json()
    attrs = body.get("data", {}).get("attributes", {})
    stats = attrs.get("last_analysis_stats", {})
    lad = attrs.get("last_analysis_date")
    if lad and isinstance(lad, int):
        lad = datetime.fromtimestamp(lad, tz=timezone.utc).isoformat()
    engines_total = sum(stats.values()) if stats else 0
    return _map_stats(stats, lad, engines_total), stats, lad


async def _vt_upload_and_poll(
    path: Path, sha256: str, client: httpx.AsyncClient
) -> tuple[Verdict, dict, str | None]:
    """POST /api/v3/files then poll /api/v3/analyses/{id}."""
    if path.stat().st_size > _VT_UPLOAD_CAP_BYTES:
        return "unverified", {}, None  # reason = too_large handled by caller

    if not await _vt_daily_check():
        return "unverified", {}, None
    await _vt_acquire()

    try:
        with path.open("rb") as fh:
            r = await client.post(
                f"{_VT_BASE}/files",
                headers={"x-apikey": settings.VT_API_KEY},
                files={"file": (path.name, fh, "application/octet-stream")},
                timeout=120.0,
            )
    except Exception as exc:
        logger.warning(json.dumps({"kind": "vt_upload_error", "detail": str(exc)}))
        return "unverified", {}, None

    if r.status_code not in (200, 201):
        return "unverified", {}, None

    analysis_id = r.json().get("data", {}).get("id")
    if not analysis_id:
        return "unverified", {}, None

    # Poll with a 4-minute hard cap.
    poll_url = f"{_VT_BASE}/analyses/{analysis_id}"
    deadline = time.monotonic() + 240

    while time.monotonic() < deadline:
        await asyncio.sleep(25)
        if not await _vt_daily_check():
            return "unverified", {}, None
        await _vt_acquire()

        try:
            pr = await client.get(
                poll_url,
                headers={"x-apikey": settings.VT_API_KEY},
                timeout=30.0,
            )
        except Exception:
            continue

        if pr.status_code != 200:
            continue

        body = pr.json()
        attrs = body.get("data", {}).get("attributes", {})
        if attrs.get("status") != "completed":
            continue

        stats = attrs.get("stats", {})
        engines_total = sum(stats.values()) if stats else 0
        lad = datetime.now(timezone.utc).isoformat()
        return _map_stats(stats, lad, engines_total), stats, lad

    return "unverified", {}, None  # scan_timeout


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def verify_and_scan(job_id: str, path: Path, ext: str) -> dict:
    """
    Run format verify then VT scan. Updates job status throughout.
    Returns {"verdict": ..., "sha256": ..., "has_active_content": ..., "reason": ...}.
    """
    # Format verification (in the main process, before VT).
    await job_svc.update_job(job_id, status="verifying")
    fmt = verify_format(path, ext)
    if not fmt["ok"]:
        path.unlink(missing_ok=True)
        await job_svc.update_job(job_id, status="blocked", reason=fmt["reason"])
        return {"verdict": "blocked", "reason": fmt["reason"]}

    has_active = fmt.get("has_active_content", False)

    if not settings.VT_API_KEY:
        # No VT key configured: treat as unverified (block-all-unscanned policy).
        path.unlink(missing_ok=True)
        await job_svc.update_job(job_id, status="unverified", reason="no_vt_key")
        return {"verdict": "unverified", "reason": "no_vt_key"}

    await job_svc.update_job(job_id, status="scanning")

    sem = _get_scan_sem()
    async with sem:
        sha256 = sha256_file(path)

        # Cache lookup.
        cached = await _cache_lookup(sha256)
        if cached:
            verdict = cached["verdict"]
            stats = {
                "malicious": cached["malicious_count"],
                "suspicious": cached["suspicious_count"],
                "total": cached["engines_total"],
            }
            return await _apply_verdict(job_id, path, ext, verdict, sha256, stats, has_active)

        async with httpx.AsyncClient() as client:
            verdict, stats, lad = await _vt_lookup(sha256, client)

            if verdict == "not_found":  # type: ignore[comparison-overlap]
                if path.stat().st_size > _VT_UPLOAD_CAP_BYTES:
                    verdict = "unverified"
                    lad = None
                else:
                    verdict, stats, lad = await _vt_upload_and_poll(path, sha256, client)

        # Cache the result if we have useful data.
        if verdict in ("clean", "malicious", "suspicious") and stats:
            await _cache_store(sha256, verdict, stats, lad)

    return await _apply_verdict(job_id, path, ext, verdict, sha256, stats, has_active)


async def _apply_verdict(
    job_id: str,
    path: Path,
    ext: str,
    verdict: str,
    sha256: str,
    stats: dict,
    has_active_content: bool,
) -> dict:
    ready_dir = Path(settings.DATA_DIR) / "ready"
    ready_dir.mkdir(parents=True, exist_ok=True)

    if verdict == "clean":
        dest = ready_dir / f"{job_id}.{ext}"
        path.rename(dest)
        await job_svc.update_job(
            job_id,
            status="clean",
            download_url=f"/api/files/{job_id}",
        )
        return {
            "verdict": "clean",
            "sha256": sha256,
            "has_active_content": has_active_content,
        }

    if verdict in ("malicious", "suspicious"):
        path.unlink(missing_ok=True)
        detail = json.dumps(stats)
        await job_svc.update_job(job_id, status="blocked", reason=verdict, detail=detail)
        return {"verdict": "blocked", "sha256": sha256, "reason": verdict}

    # unverified
    path.unlink(missing_ok=True)
    reason = "quota" if not stats else "scan_timeout"
    await job_svc.update_job(job_id, status="unverified", reason=reason)
    return {"verdict": "unverified", "sha256": sha256, "reason": reason}
