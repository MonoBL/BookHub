"""TTL cleaner: sweeps quarantine/ready dirs and prunes DB. See BUILD.md §7.6."""
import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from app.config import settings
from app.db import write_db
from app.services import jobs as job_svc

logger = logging.getLogger("bookhub")


async def _sweep_dir(directory: Path, ttl_seconds: int) -> int:
    """Delete files older than ttl_seconds, skipping in-flight serves. Returns count."""
    in_flight = await job_svc.in_flight_snapshot()
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
    removed = 0

    if not directory.exists():
        return 0

    for f in directory.iterdir():
        if not f.is_file():
            continue
        job_id = f.stem  # "UUID4.epub" -> "UUID4"
        if job_id in in_flight:
            continue
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                f.unlink(missing_ok=True)
                removed += 1
        except Exception as exc:
            logger.warning(json.dumps({"kind": "sweep_error", "file": str(f), "detail": str(exc)}))

    return removed


async def _prune_db() -> None:
    """Prune sessions, cap history and events. See BUILD.md §5."""
    now_iso = datetime.now(timezone.utc).isoformat()
    cutoff_90d = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()

    async with write_db() as db:
        # Expired sessions.
        await db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_iso,))

        # Cap history to latest 500 rows per user.
        await db.execute(
            """
            DELETE FROM history WHERE id NOT IN (
                SELECT id FROM history h2
                WHERE h2.user_id = history.user_id
                ORDER BY created_at DESC LIMIT 500
            )
            """
        )

        # Cap events: keep 10k rows or last 90 days.
        await db.execute(
            "DELETE FROM events WHERE ts < ? AND id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 10000)",
            (cutoff_90d,),
        )


async def run_cleaner() -> None:
    """Background task: sweeps files and prunes DB every 5 minutes."""
    ttl_s = settings.FILE_TTL_MINUTES * 60
    quarantine = Path(settings.DATA_DIR) / "quarantine"
    ready = Path(settings.DATA_DIR) / "ready"

    while True:
        await asyncio.sleep(300)
        try:
            removed_q = await _sweep_dir(quarantine, ttl_s)
            removed_r = await _sweep_dir(ready, ttl_s)
            if removed_q + removed_r:
                logger.info(json.dumps({
                    "kind": "sweep",
                    "quarantine_removed": removed_q,
                    "ready_removed": removed_r,
                }))
            await _prune_db()
        except Exception as exc:
            logger.error(json.dumps({"kind": "cleaner_error", "detail": str(exc)}))
