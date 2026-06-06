import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from app.config import settings

_write_lock = asyncio.Lock()

_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vt_cache (
    sha256 TEXT PRIMARY KEY,
    verdict TEXT NOT NULL,
    malicious_count INTEGER,
    suspicious_count INTEGER,
    engines_total INTEGER,
    last_analysis_date TEXT,
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    title TEXT,
    author TEXT,
    source TEXT,
    ext TEXT,
    sha256 TEXT,
    verdict TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_history_user ON history(user_id, created_at);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    user_id INTEGER,
    title TEXT,
    source TEXT,
    sha256 TEXT,
    detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


def _db_path() -> Path:
    return Path(settings.DATA_DIR) / "app.db"


async def init_db() -> None:
    data_dir = Path(settings.DATA_DIR)
    for subdir in ("", "quarantine", "ready", "jobs"):
        (data_dir / subdir).mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(_db_path()) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.executescript(_DDL)


@asynccontextmanager
async def get_db():
    """Read context. No commit."""
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("PRAGMA synchronous=NORMAL")
        yield db


@asynccontextmanager
async def write_db():
    """Write context. Serialized via asyncio.Lock; commits on exit."""
    async with _write_lock:
        async with aiosqlite.connect(_db_path()) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("PRAGMA synchronous=NORMAL")
            yield db
            await db.commit()
