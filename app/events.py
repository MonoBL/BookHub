import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("bookhub")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(kind: str, **kwargs) -> None:
    """Emit a structured JSON log line. No secrets."""
    record = {"ts": _now_iso(), "kind": kind, **kwargs}
    logger.info(json.dumps(record))


# DB write for events table is implemented in M8 alongside the admin events view.
# For now, log_event only writes to structured stdout.
