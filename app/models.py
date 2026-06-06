from pydantic import BaseModel


class SearchResult(BaseModel):
    id: str
    title: str
    author: str | None = None
    ext: str
    size_bytes: int | None = None
    source: str
    extra: dict = {}


class Job(BaseModel):
    id: str
    status: str
    ext: str | None = None
    title: str | None = None
    download_url: str | None = None
    reason: str | None = None
    detail: str | None = None
    user_id: int | None = None
    source: str | None = None
    author: str | None = None


class User(BaseModel):
    id: int
    username: str
    is_admin: bool
    must_change_password: bool
    created_at: str


class Event(BaseModel):
    id: int
    ts: str
    kind: str
    user_id: int | None = None
    title: str | None = None
    source: str | None = None
    sha256: str | None = None
    detail: str | None = None
