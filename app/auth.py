import asyncio
import secrets
import time
from datetime import datetime, timezone, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request

from app.config import settings
from app.db import get_db, write_db

_ph = PasswordHasher()

# Pre-computed dummy hash used to keep login response time constant for
# unknown usernames (prevents timing-based user enumeration).
_DUMMY_HASH = _ph.hash("x")

SESSION_TTL_DAYS = settings.SESSION_TTL_DAYS
_RATE_WINDOW_S = 60
_RATE_MAX_FAILURES = 5

_failure_timestamps: dict[str, list[float]] = {}
_rl_lock = asyncio.Lock()

# These paths are exempt from the must_change_password gate so the form
# and logout can still function when a forced change is pending.
_MUST_CHANGE_EXEMPT = frozenset({"/api/auth/change-password", "/api/auth/logout"})


# --- Password hashing ---

def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        _ph.verify(password_hash, password)
        return True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# --- Rate limiting ---

def _clear_rate_limits() -> None:
    """Test helper: clears all in-memory rate-limit state."""
    _failure_timestamps.clear()


async def check_rate_limit(rl_key: str) -> None:
    """Raise 429 if >= 5 failures in the past 60 s for this key."""
    async with _rl_lock:
        now = time.monotonic()
        recent = [t for t in _failure_timestamps.get(rl_key, []) if now - t < _RATE_WINDOW_S]
        _failure_timestamps[rl_key] = recent
        if len(recent) >= _RATE_MAX_FAILURES:
            raise HTTPException(status_code=429, detail="Too many login attempts, try again later")


async def record_login_failure(rl_key: str) -> None:
    async with _rl_lock:
        now = time.monotonic()
        bucket = _failure_timestamps.get(rl_key, [])
        bucket.append(now)
        _failure_timestamps[rl_key] = [t for t in bucket if now - t < _RATE_WINDOW_S]


async def reset_rate_limit(rl_key: str) -> None:
    """Clear the failure bucket for this key on successful login."""
    async with _rl_lock:
        _failure_timestamps.pop(rl_key, None)


# --- First-run setup ---

async def user_count() -> int:
    """Number of users. 0 means the app needs first-run admin setup."""
    async with get_db() as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        (count,) = await cur.fetchone()
    return count


# --- Sessions ---

async def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now_iso = datetime.now(timezone.utc).isoformat()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)).isoformat()
    async with write_db() as db:
        # Opportunistically prune expired sessions to keep the table lean.
        await db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_iso,))
        await db.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
            (token, user_id, expires_at),
        )
    return token


async def get_session_user(token: str) -> dict | None:
    """Return {id, username, is_admin, must_change_password} for a valid, unexpired token."""
    now = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT u.id, u.username, u.is_admin, u.must_change_password
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token = ? AND s.expires_at > ?
            """,
            (token, now),
        )
        row = await cur.fetchone()
    return dict(row) if row else None


async def delete_session(token: str) -> None:
    async with write_db() as db:
        await db.execute("DELETE FROM sessions WHERE token = ?", (token,))


# --- FastAPI dependencies ---

async def require_user(request: Request) -> dict:
    """Validates session cookie. Raises 401 if missing/invalid.
    Raises 403 if must_change_password and path is not exempt."""
    token = request.cookies.get("session")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = await get_session_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if user["must_change_password"] and request.url.path not in _MUST_CHANGE_EXEMPT:
        raise HTTPException(status_code=403, detail="Password change required before continuing")
    return user


async def require_admin(request: Request) -> dict:
    """Requires a valid admin session. Raises 403 for non-admins."""
    user = await require_user(request)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user
