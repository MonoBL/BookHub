import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Depends
from fastapi.staticfiles import StaticFiles

from app import auth as _auth
from app.auth import require_user
from app.config import settings
from app.db import get_db, init_db, write_db
from app.routers import api_admin, api_auth, api_convert, api_files, api_jobs, api_search, pages
from app.events import start_events_writer, stop_events_writer
from app.services.cleaner import run_cleaner

logging.basicConfig(level=logging.INFO, format="%(message)s")


async def _bootstrap_admin() -> None:
    """Create the admin user on first run (users table empty). See BUILD.md §5."""
    async with get_db() as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        (count,) = await cur.fetchone()

    if count > 0:
        return

    password = settings.ADMIN_PASSWORD
    must_change = 0

    if not password or len(password) < 12:
        if password:
            print(
                "[BookHub] ADMIN_PASSWORD is shorter than 12 chars; ignoring, using random.",
                flush=True,
            )
        password = secrets.token_urlsafe(16)
        must_change = 1
        # Documented exception: password printed once to stdout for operator retrieval.
        print(f"[BookHub] First-run admin password: {password}", flush=True)

    now = datetime.now(timezone.utc).isoformat()
    password_hash = _auth.hash_password(password)

    async with write_db() as db:
        await db.execute(
            "INSERT INTO users (username, password_hash, is_admin, must_change_password, created_at)"
            " VALUES ('admin', ?, 1, ?, ?)",
            (password_hash, must_change, now),
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await _bootstrap_admin()
    start_events_writer()
    cleaner_task = asyncio.create_task(run_cleaner())
    yield
    cleaner_task.cancel()
    stop_events_writer()
    try:
        await cleaner_task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="BookHub", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(pages.router)
app.include_router(api_auth.router, prefix="/api/auth", tags=["auth"])

# All /api routes below require a valid session (cross-cutting auth rule §A).
app.include_router(
    api_search.router, prefix="/api", tags=["search"],
    dependencies=[Depends(require_user)],
)
app.include_router(
    api_jobs.router, prefix="/api", tags=["jobs"],
    dependencies=[Depends(require_user)],
)
app.include_router(
    api_convert.router, prefix="/api", tags=["convert"],
    dependencies=[Depends(require_user)],
)
app.include_router(
    api_files.router, prefix="/api", tags=["files"],
    dependencies=[Depends(require_user)],
)
app.include_router(api_admin.router, prefix="/api/admin", tags=["admin"])


@app.get("/api/health")
async def health():
    return {"status": "ok"}
