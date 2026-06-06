"""Structured logging + events-table audit writer. See BUILD.md §12."""
import asyncio
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("bookhub")

# Background write queue: keeps the write serialized and non-blocking.
_queue: asyncio.Queue | None = None
_writer_task: asyncio.Task | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(
    kind: str,
    *,
    user_id: int | None = None,
    title: str | None = None,
    source: str | None = None,
    sha256: str | None = None,
    **kwargs,
) -> None:
    """Emit a structured JSON log line AND queue a DB write. No secrets."""
    ts = _now_iso()
    detail = json.dumps(kwargs) if kwargs else None
    record = {"ts": ts, "kind": kind}
    if user_id is not None:
        record["user_id"] = user_id
    if title:
        record["title"] = title
    if source:
        record["source"] = source
    if sha256:
        record["sha256"] = sha256
    if detail:
        record["detail"] = detail
    logger.info(json.dumps(record))

    # Enqueue DB write (best-effort; if queue not started, skip silently).
    if _queue is not None:
        try:
            _queue.put_nowait((ts, kind, user_id, title, source, sha256, detail))
        except asyncio.QueueFull:
            logger.warning("events queue full, dropping event kind=%s", kind)


async def _writer_loop() -> None:
    from app.db import write_db
    while True:
        item = await _queue.get()
        ts, kind, user_id, title, source, sha256, detail = item
        try:
            async with write_db() as db:
                await db.execute(
                    "INSERT INTO events (ts, kind, user_id, title, source, sha256, detail)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (ts, kind, user_id, title, source, sha256, detail),
                )
        except Exception as exc:
            logger.warning("events DB write failed: %s", exc)
        finally:
            _queue.task_done()


def start_events_writer() -> None:
    """Start the background writer. Called once from lifespan."""
    global _queue, _writer_task
    _queue = asyncio.Queue(maxsize=1000)
    _writer_task = asyncio.create_task(_writer_loop())


def stop_events_writer() -> None:
    """Cancel the writer. Called from lifespan on shutdown."""
    if _writer_task:
        _writer_task.cancel()
