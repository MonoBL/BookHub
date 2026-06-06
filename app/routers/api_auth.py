from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app import auth
from app.config import settings
from app.db import get_db, write_db

router = APIRouter()


class LoginBody(BaseModel):
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


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response):
    key = (request.client.host if request.client else None) or "unknown"
    await auth.check_rate_limit(key)

    async with get_db() as db:
        cur = await db.execute(
            "SELECT id, password_hash, must_change_password FROM users WHERE username = ?",
            (body.username,),
        )
        row = await cur.fetchone()

    if not row or not auth.verify_password(row["password_hash"], body.password):
        await auth.record_login_failure(key)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = await auth.create_session(row["id"])
    _set_session_cookie(response, token)
    return {"must_change_password": bool(row["must_change_password"])}


@router.post("/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get("session")
    if token:
        await auth.delete_session(token)
    response.delete_cookie("session", path="/")
    return {"ok": True}


@router.post("/change-password")
async def change_password(
    body: ChangePasswordBody,
    request: Request,
    user: dict = Depends(auth.require_user),
):
    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

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

    return {"ok": True}
