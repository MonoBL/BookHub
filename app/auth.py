# Auth implementation: M2.
# Stubs here so other modules can import without crashing.
from fastapi import Request, HTTPException


async def require_user(request: Request):
    """Dependency: validates session cookie, returns user row. Implemented in M2."""
    raise HTTPException(status_code=401, detail="Not authenticated")


async def require_admin(request: Request):
    """Dependency: requires admin session. Implemented in M2."""
    raise HTTPException(status_code=401, detail="Not authenticated")
