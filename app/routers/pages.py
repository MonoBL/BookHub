from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter()

# Auth redirects added in M2. For now: serve pages directly.

@router.get("/")
async def index():
    return FileResponse("static/index.html")


@router.get("/login")
async def login_page():
    return FileResponse("static/login.html")


@router.get("/convert")
async def convert_page():
    return FileResponse("static/convert.html")


@router.get("/admin")
async def admin_page():
    return FileResponse("static/admin.html")


@router.get("/change-password")
async def change_password_page():
    return FileResponse("static/change-password.html")


@router.get("/sw.js")
async def service_worker():
    # Service-Worker-Allowed: / lets the SW control the full origin scope.
    return FileResponse(
        "static/sw.js",
        headers={"Service-Worker-Allowed": "/"},
        media_type="application/javascript",
    )


@router.get("/manifest.webmanifest")
async def manifest():
    return FileResponse("static/manifest.webmanifest", media_type="application/manifest+json")
