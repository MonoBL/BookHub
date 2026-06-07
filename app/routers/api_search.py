"""Search routes. See BUILD.md §6.2."""
from fastapi import APIRouter, Depends, Query
from typing import Optional

from app.auth import require_user
from app.services.search import search as do_search

router = APIRouter()


@router.get("/search")
async def search(
    q: str = Query(..., min_length=1, max_length=200),
    ext: Optional[str] = Query(None, regex="^(epub|pdf|both)?$"),
    user: dict = Depends(require_user),
):
    ext_filter: list[str] = []
    if ext == "epub":
        ext_filter = ["epub"]
    elif ext == "pdf":
        ext_filter = ["pdf"]
    # "both" or None -> no filter, all types

    return await do_search(q, ext_filter)
