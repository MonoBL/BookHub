from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, RedirectResponse

from app import auth

router = APIRouter()


async def _page_user(request: Request) -> dict | None:
    """Returns session user or None without raising. Used by page handlers for redirects."""
    token = request.cookies.get("session")
    if not token:
        return None
    return await auth.get_session_user(token)


@router.get("/")
async def index(request: Request):
    user = await _page_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user["must_change_password"]:
        return RedirectResponse("/change-password", status_code=302)
    return FileResponse("static/index.html")


@router.get("/login")
async def login_page(request: Request):
    user = await _page_user(request)
    if user:
        target = "/change-password" if user["must_change_password"] else "/"
        return RedirectResponse(target, status_code=302)
    return FileResponse("static/login.html")


@router.get("/convert")
async def convert_page(request: Request):
    user = await _page_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user["must_change_password"]:
        return RedirectResponse("/change-password", status_code=302)
    return FileResponse("static/convert.html")


@router.get("/admin")
async def admin_page(request: Request):
    user = await _page_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user["must_change_password"]:
        return RedirectResponse("/change-password", status_code=302)
    if not user["is_admin"]:
        return RedirectResponse("/", status_code=302)
    return FileResponse("static/admin.html")


@router.get("/change-password")
async def change_password_page(request: Request):
    user = await _page_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return FileResponse("static/change-password.html")


@router.get("/sw.js")
async def service_worker():
    return FileResponse(
        "static/sw.js",
        headers={"Service-Worker-Allowed": "/"},
        media_type="application/javascript",
    )


@router.get("/manifest.webmanifest")
async def manifest():
    return FileResponse("static/manifest.webmanifest", media_type="application/manifest+json")
