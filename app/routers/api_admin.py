from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import auth
from app.db import get_db, write_db

router = APIRouter()


class CreateUserBody(BaseModel):
    username: str
    password: str
    is_admin: bool = False


class ResetPasswordBody(BaseModel):
    new_password: str


@router.get("/users")
async def list_users(_user: dict = Depends(auth.require_admin)):
    async with get_db() as db:
        cur = await db.execute(
            "SELECT id, username, is_admin, must_change_password, created_at FROM users ORDER BY id"
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


@router.post("/users")
async def create_user(body: CreateUserBody, _user: dict = Depends(auth.require_admin)):
    if not body.username.strip():
        raise HTTPException(status_code=400, detail="Username cannot be empty")
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    now = datetime.now(timezone.utc).isoformat()
    password_hash = auth.hash_password(body.password)

    try:
        async with write_db() as db:
            cur = await db.execute(
                "INSERT INTO users (username, password_hash, is_admin, must_change_password, created_at)"
                " VALUES (?, ?, ?, 0, ?)",
                (body.username.strip(), password_hash, int(body.is_admin), now),
            )
            user_id = cur.lastrowid
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(status_code=409, detail="Username already exists")
        raise

    return {
        "id": user_id,
        "username": body.username.strip(),
        "is_admin": body.is_admin,
        "must_change_password": False,
    }


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, user: dict = Depends(auth.require_admin)):
    if user_id == user["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    async with write_db() as db:
        # Invalidate sessions first so the deleted user is logged out immediately.
        await db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM users WHERE id = ?", (user_id,))

    return {"ok": True}


@router.post("/users/{user_id}/reset-password")
async def reset_password(
    user_id: int,
    body: ResetPasswordBody,
    _user: dict = Depends(auth.require_admin),
):
    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    new_hash = auth.hash_password(body.new_password)
    async with write_db() as db:
        await db.execute(
            "UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?",
            (new_hash, user_id),
        )
        # Invalidate existing sessions to force re-login with the new password.
        await db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    return {"ok": True}
