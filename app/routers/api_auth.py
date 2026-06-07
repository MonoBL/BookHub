import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app import auth
from app.config import settings
from app.db import get_db, write_db

router = APIRouter()

_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")


class LoginBody(BaseModel):
    username: str
    password: str


class SetupBody(BaseModel):
    username: str
    password: str


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str


def _set_session_cookie(response: Response, token: str) -> None:
    kwargs: dict = dict(
        key="session",
        value=token,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=30 * 24 * 3600,
    )
    if settings.COOKIE_SECURE:
        kwargs["secure"] = True
    response.set_cookie(**kwargs)


@router.get("/needs-setup")
async def needs_setup():
    """True when no users exist yet (first-run admin creation screen)."""
    return {"needs_setup": await auth.user_count() == 0}


@router.post("/setup")
async def setup(body: SetupBody, response: Response):
    """Create the first admin on a fresh install. Disabled once any user exists."""
    if not _USERNAME_RE.match(body.username):
        raise HTTPException(
            status_code=400,
            detail="Username must be 3-32 chars: letters, numbers, . _ -",
        )
    if len(body.password) < 12:
        raise HTTPException(status_code=400, detail="Password must be at least 12 characters")

    now = datetime.now(timezone.utc).isoformat()
    pw_hash = auth.hash_password(body.password)

    # write_db() serializes writes, so the count+insert is race-safe.
    async with write_db() as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        (count,) = await cur.fetchone()
        if count > 0:
            raise HTTPException(status_code=403, detail="Setup already completed")
        cur = await db.execute(
            "INSERT INTO users (username, password_hash, is_admin, must_change_password, created_at)"
            " VALUES (?, ?, 1, 0, ?)",
            (body.username, pw_hash, now),
        )
        user_id = cur.lastrowid

    token = await auth.create_session(user_id)
    _set_session_cookie(response, token)
    return {"ok": True}


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response):
    # Cloudflare Tunnel is the only ingress; prefer CF-Connecting-IP.
    key = (
        request.headers.get("cf-connecting-ip")
        or (request.client.host if request.client else None)
        or "unknown"
    )
    rl_key = f"{key}:{body.username}"
    await auth.check_rate_limit(rl_key)

    async with get_db() as db:
        cur = await db.execute(
            "SELECT id, password_hash, must_change_password FROM users WHERE username = ?",
            (body.username,),
        )
        row = await cur.fetchone()

    # Always run verify_password so timing is constant whether the user exists or not.
    pw_hash = row["password_hash"] if row else auth._DUMMY_HASH
    ok = auth.verify_password(pw_hash, body.password)

    if not ok or row is None:
        await auth.record_login_failure(rl_key)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    await auth.reset_rate_limit(rl_key)
    token = await auth.create_session(row["id"])
    _set_session_cookie(response, token)
    return {"must_change_password": bool(row["must_change_password"])}


@router.post("/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get("session")
    if token:
        await auth.delete_session(token)
    response.delete_cookie(
        "session",
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
    )
    return {"ok": True}


@router.post("/change-password")
async def change_password(
    body: ChangePasswordBody,
    request: Request,
    user: dict = Depends(auth.require_user),
):
    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

    current_token = request.cookies.get("session")

    async with get_db() as db:
        cur = await db.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],))
        row = await cur.fetchone()

    if not row or not auth.verify_password(row["password_hash"], body.current_password):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    new_hash = auth.hash_password(body.new_password)
    async with write_db() as db:
        await db.execute(
            "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
            (new_hash, user["id"]),
        )
        # Invalidate all OTHER sessions so concurrent devices are forced to re-login.
        # The current session stays valid so the user is not immediately logged out.
        await db.execute(
            "DELETE FROM sessions WHERE user_id = ? AND token != ?",
            (user["id"], current_token),
        )

    return {"ok": True}
