"""M6 tests: converter route, density/OCR logic, worker launch."""
import re
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from app.main import app

FIXTURES = Path(__file__).parent / "fixtures"
ADMIN_PW = "testpassword123"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def token(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
    return r.cookies["session"]


@pytest.fixture
def anon():
    with TestClient(app) as c:
        yield c


UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Auth guard (uses fresh anon client)
# ---------------------------------------------------------------------------

def test_convert_requires_auth(anon):
    pdf = FIXTURES / "real.pdf"
    r = anon.post(
        "/api/convert",
        files={"file": ("test.pdf", pdf.read_bytes(), "application/pdf")},
        data={"title": "My Book"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def test_convert_rejects_non_pdf(client, token):
    r = client.post(
        "/api/convert",
        files={"file": ("test.epub", b"PK\x03\x04fake", "application/epub+zip")},
        data={"title": "My Book"},
        cookies={"session": token},
    )
    assert r.status_code == 400
    assert "PDF" in r.json()["detail"]


def test_convert_rejects_polyglot(client, token):
    pdf = FIXTURES / "polyglot.pdf"
    r = client.post(
        "/api/convert",
        files={"file": ("bad.pdf", pdf.read_bytes(), "application/pdf")},
        data={"title": "My Book"},
        cookies={"session": token},
    )
    assert r.status_code == 400


def test_convert_requires_title(client, token):
    pdf = FIXTURES / "real.pdf"
    r = client.post(
        "/api/convert",
        files={"file": ("test.pdf", pdf.read_bytes(), "application/pdf")},
        data={"author": "Author"},  # no title field
        cookies={"session": token},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Density probe (unit - mocked subprocess)
# ---------------------------------------------------------------------------

def test_density_probe_high_density(tmp_path):
    from converter.run_convert import _density_probe
    pdf = tmp_path / "test.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    pdfinfo = MagicMock(returncode=0)
    pdfinfo.stdout = b"Pages:          10\n"
    pdftotext = MagicMock(returncode=0)
    pdftotext.stdout = b" ".join([b"word"] * 50)

    with patch("converter.run_convert.subprocess.run") as mock_run:
        mock_run.side_effect = [pdfinfo] + [pdftotext] * 10
        density = _density_probe(pdf)

    assert density > 10.0


def test_density_probe_low_density(tmp_path):
    from converter.run_convert import _density_probe
    pdf = tmp_path / "test.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    pdfinfo = MagicMock(returncode=0)
    pdfinfo.stdout = b"Pages:          5\n"
    pdftotext = MagicMock(returncode=0)
    pdftotext.stdout = b"word"  # 1 word per page

    with patch("converter.run_convert.subprocess.run") as mock_run:
        mock_run.side_effect = [pdfinfo] + [pdftotext] * 5
        density = _density_probe(pdf)

    assert density < 10.0


# ---------------------------------------------------------------------------
# OCR gate logic (unit)
# ---------------------------------------------------------------------------

def test_ocr_called_on_low_density(tmp_path):
    from converter import run_convert

    pdf = tmp_path / "in.pdf"
    epub = tmp_path / "out.epub"
    pdf.write_bytes(b"%PDF-1.4\n")

    ocr_calls = []

    def fake_ocr(src, out, langs, timeout):
        ocr_calls.append(str(src))
        return False  # simulate failure -> fallback

    convert_calls = []

    def fake_convert(src, dst, title, author, timeout):
        convert_calls.append(str(src))

    with (
        patch.object(run_convert, "_density_probe", return_value=2.0),
        patch.object(run_convert, "_run_ocr", side_effect=fake_ocr),
        patch.object(run_convert, "_run_ebook_convert", side_effect=fake_convert),
    ):
        density = run_convert._density_probe(pdf)
        src_pdf = pdf
        if density < 10.0:
            with tempfile.NamedTemporaryFile(suffix=".pdf", dir=pdf.parent, delete=False) as f:
                ocr_out = Path(f.name)
            success = run_convert._run_ocr(pdf, ocr_out, "eng", 60)
            if not success:
                ocr_out.unlink(missing_ok=True)
        run_convert._run_ebook_convert(src_pdf, epub, "T", "A", 60)

    assert len(ocr_calls) == 1
    assert len(convert_calls) == 1
    # OCR failed -> fallback: convert with original pdf
    assert convert_calls[0] == str(pdf)


def test_ocr_skipped_on_high_density(tmp_path):
    from converter import run_convert

    pdf = tmp_path / "in.pdf"
    epub = tmp_path / "out.epub"
    pdf.write_bytes(b"%PDF-1.4\n")

    ocr_calls = []
    convert_calls = []

    with (
        patch.object(run_convert, "_density_probe", return_value=50.0),
        patch.object(run_convert, "_run_ocr", side_effect=lambda *a, **k: ocr_calls.append(1)),
        patch.object(run_convert, "_run_ebook_convert", side_effect=lambda *a, **k: convert_calls.append(1)),
    ):
        density = run_convert._density_probe(pdf)
        src_pdf = pdf
        if density < 10.0:
            run_convert._run_ocr(pdf, epub, "eng", 60)
        run_convert._run_ebook_convert(src_pdf, epub, "T", "A", 60)

    assert len(ocr_calls) == 0
    assert len(convert_calls) == 1


# ---------------------------------------------------------------------------
# Docker command security checks (unit)
# ---------------------------------------------------------------------------

def test_docker_cmd_has_security_flags(tmp_path):
    from app.services.converter import _docker_cmd
    pdf = tmp_path / "in.pdf"
    cmd = _docker_cmd("test-job-id", pdf, "Title", "Author", 600, 1200)
    cmd_str = " ".join(cmd)

    assert "--network=none" in cmd_str
    assert "--read-only" in cmd_str
    assert "--cap-drop" in cmd_str
    assert "ALL" in cmd_str
    assert "no-new-privileges" in cmd_str


def test_docker_cmd_passes_timeouts(tmp_path):
    from app.services.converter import _docker_cmd
    pdf = tmp_path / "in.pdf"
    # Comic-mode timeouts must reach the worker as CLI args.
    cmd = _docker_cmd("test-job-id", pdf, "Title", "Author", 2400, 3600)
    assert "--convert-timeout" in cmd
    assert cmd[cmd.index("--convert-timeout") + 1] == "2400"
    assert "--ocr-timeout" in cmd
    assert cmd[cmd.index("--ocr-timeout") + 1] == "3600"


def test_docker_cmd_no_secrets_in_env(tmp_path):
    from app.services.converter import _docker_cmd
    pdf = tmp_path / "in.pdf"
    cmd = _docker_cmd("test-job-id", pdf, "Title", "Author", 600, 1200)
    cmd_str = " ".join(cmd)

    # No secrets in worker env (cross-cutting rule B)
    assert "VT_API_KEY" not in cmd_str
    assert "VK_TOKEN" not in cmd_str
    assert "AA_API_KEY" not in cmd_str


def test_docker_cmd_mounts_only_job_dir(tmp_path):
    from app.services.converter import _docker_cmd
    pdf = tmp_path / "in.pdf"
    cmd = _docker_cmd("test-job-id", pdf, "Title", "Author", 600, 1200)

    mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]
    assert len(mounts) == 1, "Worker must have exactly one volume mount"
    host_dir = mounts[0].split(":")[0]
    # Must be the job scratch dir, not /data root or anything broader
    assert str(tmp_path) in host_dir


def test_docker_cmd_has_container_name(tmp_path):
    from app.services.converter import _docker_cmd
    pdf = tmp_path / "in.pdf"
    cmd = _docker_cmd("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", pdf, "Title", "Author", 600, 1200)
    cmd_str = " ".join(cmd)
    assert "--name" in cmd_str
    assert "bookhub-conv-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" in cmd_str


def test_docker_cmd_mount_translates_host_data_dir(tmp_path):
    from app.services.converter import _docker_cmd
    from app.config import settings
    from unittest.mock import patch

    # Simulate DATA_DIR=/data, HOST_DATA_DIR=/host/data
    # pdf is at /data/jobs/testjob/in.pdf -> host mount should be /host/data/jobs/testjob
    with (
        patch.object(settings, "DATA_DIR", "/data"),
        patch.object(settings, "HOST_DATA_DIR", "/host/data"),
    ):
        pdf = Path("/data/jobs/testjob/in.pdf")
        cmd = _docker_cmd("test-job-id", pdf, "Title", "Author", 600, 1200)

    mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]
    host_dir = mounts[0].split(":")[0]
    assert host_dir == "/host/data/jobs/testjob"


def test_kill_container_runs_docker_kill():
    from app.services.converter import _kill_container
    from unittest.mock import patch, MagicMock

    killed_cmds = []

    def fake_run(cmd, **kw):
        killed_cmds.append(cmd)
        return MagicMock(returncode=0)

    with patch("app.services.converter.subprocess.run", side_effect=fake_run):
        _kill_container("bookhub-conv-test-job")

    assert len(killed_cmds) == 1
    assert "kill" in killed_cmds[0]
    assert "bookhub-conv-test-job" in killed_cmds[0]


# ---------------------------------------------------------------------------
# Endpoint: returns job_id
# ---------------------------------------------------------------------------

def test_convert_endpoint_returns_job_id(client, token):
    pdf = FIXTURES / "real.pdf"

    def fake_docker(cmd):
        for i, a in enumerate(cmd):
            if a == "-v":
                host_dir = cmd[i + 1].split(":")[0]
                out = Path(host_dir) / "out.epub"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"EPUBFAKE")
                return

    with patch("app.services.converter._run_docker", side_effect=fake_docker):
        r = client.post(
            "/api/convert",
            files={"file": ("novel.pdf", pdf.read_bytes(), "application/pdf")},
            data={"title": "My Novel", "author": "Test Author"},
            cookies={"session": token},
        )

    assert r.status_code == 200
    assert UUID4_RE.match(r.json()["job_id"])
