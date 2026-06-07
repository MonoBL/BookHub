"""Tests for the image -> BMP converter (service + route + downloads list)."""
import io
import zipfile
from pathlib import Path

import pytest
from PIL import Image
from starlette.testclient import TestClient

from app.main import app
from app.services.bmp import convert_images_to_bmp

ADMIN_PW = "testpassword123"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def token(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
    return r.cookies["session"]


def _png_bytes(w=200, h=300, color=(120, 30, 200)):
    im = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _jpg_bytes(w=200, h=300):
    im = Image.new("RGB", (w, h), (10, 200, 50))
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Service unit tests
# ---------------------------------------------------------------------------

def test_single_image_makes_bmp_with_exact_size(tmp_path):
    ext = convert_images_to_bmp([("cover.png", _png_bytes())], tmp_path, 480, 800)
    assert ext == "bmp"
    out = tmp_path / "out.bmp"
    assert out.exists()
    with Image.open(out) as im:
        assert im.size == (480, 800)
        assert im.mode == "L"  # 8-bit grayscale by default


def test_color_kept_when_grayscale_false(tmp_path):
    convert_images_to_bmp([("c.png", _png_bytes())], tmp_path, 100, 100, grayscale=False)
    with Image.open(tmp_path / "out.bmp") as im:
        assert im.mode != "L"


def test_many_images_make_zip(tmp_path):
    imgs = [("a.png", _png_bytes()), ("b.jpg", _jpg_bytes())]
    ext = convert_images_to_bmp(imgs, tmp_path, 480, 800)
    assert ext == "zip"
    with zipfile.ZipFile(tmp_path / "out.zip") as zf:
        names = zf.namelist()
    assert len(names) == 2
    assert all(n.endswith(".bmp") for n in names)


def test_all_bad_images_raises(tmp_path):
    with pytest.raises(RuntimeError):
        convert_images_to_bmp([("x.png", b"not an image")], tmp_path, 480, 800)


# ---------------------------------------------------------------------------
# Route tests
# ---------------------------------------------------------------------------

def test_convert_bmp_requires_auth(client):
    r = client.post(
        "/api/convert-bmp",
        files={"files": ("a.png", _png_bytes(), "image/png")},
    )
    assert r.status_code == 401


def test_convert_bmp_rejects_non_image(client, token):
    r = client.post(
        "/api/convert-bmp",
        files={"files": ("a.png", b"definitely not an image", "image/png")},
        cookies={"session": token},
    )
    assert r.status_code == 400


def test_convert_bmp_end_to_end(client, token):
    r = client.post(
        "/api/convert-bmp",
        files={"files": ("cover.png", _png_bytes(), "image/png")},
        data={"width": "480", "height": "800"},
        cookies={"session": token},
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    # Poll the job until it settles (synchronous conversion is fast).
    import time
    for _ in range(50):
        j = client.get(f"/api/jobs/{job_id}", cookies={"session": token}).json()
        if j["status"] in ("clean", "error"):
            break
        time.sleep(0.1)
    assert j["status"] == "clean"
    assert j["ext"] == "bmp"

    # It should appear in the user's pending downloads.
    dls = client.get("/api/downloads", cookies={"session": token}).json()
    assert any(d["job_id"] == job_id and d["ext"] == "bmp" for d in dls)

    # And be downloadable, then consumed (gone from pending).
    f = client.get(f"/api/files/{job_id}", cookies={"session": token})
    assert f.status_code == 200
    assert f.content[:2] == b"BM"
    dls2 = client.get("/api/downloads", cookies={"session": token}).json()
    assert not any(d["job_id"] == job_id for d in dls2)
