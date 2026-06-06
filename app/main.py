import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.db import init_db
from app.routers import api_admin, api_auth, api_convert, api_files, api_jobs, api_search, pages

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",  # events.py emits JSON; keep format clean
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="BookHub", lifespan=lifespan)

# Static assets (CSS, JS, icons) served under /static.
# HTML pages are served via pages.py so M2 can add auth redirects.
app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(pages.router)
app.include_router(api_auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(api_search.router, prefix="/api", tags=["search"])
app.include_router(api_jobs.router, prefix="/api", tags=["jobs"])
app.include_router(api_convert.router, prefix="/api", tags=["convert"])
app.include_router(api_files.router, prefix="/api", tags=["files"])
app.include_router(api_admin.router, prefix="/api/admin", tags=["admin"])


@app.get("/api/health")
async def health():
    return {"status": "ok"}
