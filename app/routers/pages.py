import hashlib
import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app import auth

router = APIRouter()

_STATIC = Path("static")
_ASSET_RE = re.compile(r"(/static/[^\"']+\.(?:css|js))")


def _asset_version() -> str:
    """Fingerprint the static CSS/JS so each deploy yields a new asset URL.

    Cloudflare caches /static/* at the edge for hours; without a version query
    a redeploy keeps serving stale CSS/JS. Hashing file mtime+size at startup
    means a rebuilt container produces a fresh ?v=, busting the edge cache.
    """
    h = hashlib.sha1()
    for f in sorted(_STATIC.glob("*.css")) + sorted(_STATIC.glob("*.js")):
        st = f.stat()
        h.update(f"{f.name}:{int(st.st_mtime)}:{st.st_size}".encode())
    return h.hexdigest()[:8]


ASSET_VERSION = _asset_version()


def _html(path: str) -> HTMLResponse:
    """Serve an HTML page with versioned asset links and a no-cache header.

    no-cache on the HTML keeps the entry document fresh so the versioned asset
    URLs inside it are always current; the assets themselves stay cacheable.
    """
    text = Path(path).read_text(encoding="utf-8")
    text = _ASSET_RE.sub(lambda m: f"{m.group(1)}?v={ASSET_VERSION}", text)
    return HTMLResponse(text, headers={"Cache-Control": "no-cache"})


async def _page_user(request: Request) -> dict | None:
    """Returns session user or None without raising. Used by page handlers for redirects."""
    token = request.cookies.get("session")
    if not token:
        return None
    return await auth.get_session_user(token)


@router.get("/")
async def index(request: Request):
    if await auth.user_count() == 0:
        return RedirectResponse("/setup", status_code=302)
    user = await _page_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user["must_change_password"]:
        return RedirectResponse("/change-password", status_code=302)
    return _html("static/index.html")


@router.get("/setup")
async def setup_page(request: Request):
    # First-run only: once an admin exists, this screen is gone.
    if await auth.user_count() > 0:
        return RedirectResponse("/login", status_code=302)
    return _html("static/setup.html")


@router.get("/login")
async def login_page(request: Request):
    if await auth.user_count() == 0:
        return RedirectResponse("/setup", status_code=302)
    user = await _page_user(request)
    if user:
        target = "/change-password" if user["must_change_password"] else "/"
        return RedirectResponse(target, status_code=302)
    return _html("static/login.html")


@router.get("/convert")
async def convert_page(request: Request):
    user = await _page_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user["must_change_password"]:
        return RedirectResponse("/change-password", status_code=302)
    return _html("static/convert.html")


@router.get("/admin")
async def admin_page(request: Request):
    user = await _page_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user["must_change_password"]:
        return RedirectResponse("/change-password", status_code=302)
    if not user["is_admin"]:
        return RedirectResponse("/", status_code=302)
    return _html("static/admin.html")


@router.get("/change-password")
async def change_password_page(request: Request):
    user = await _page_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return _html("static/change-password.html")


@router.get("/sw.js")
async def service_worker():
    return FileResponse(
        "static/sw.js",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
        media_type="application/javascript",
    )


@router.get("/manifest.webmanifest")
async def manifest():
    return FileResponse("static/manifest.webmanifest", media_type="application/manifest+json")
