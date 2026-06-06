"""Converter orchestrator.

Launches an ephemeral gVisor-sandboxed worker via docker-socket-proxy.
LOCAL DEV: when RUNSC_RUNTIME="" in .env the --runtime flag is omitted.
"""
import asyncio
import logging
import os
import shutil
import subprocess
from pathlib import Path

from app.config import settings
from app.services import jobs as job_svc

log = logging.getLogger(__name__)

_convert_sem: asyncio.Semaphore | None = None

DOCKER_HOST = os.getenv("DOCKER_HOST", "unix:///var/run/docker.sock")


def _sem() -> asyncio.Semaphore:
    global _convert_sem
    if _convert_sem is None:
        _convert_sem = asyncio.Semaphore(settings.CONVERT_CONCURRENCY)
    return _convert_sem


def _docker_cmd(job_id: str, pdf_path: Path, title: str, author: str) -> list[str]:
    work_dir = str(pdf_path.parent)
    cmd = ["docker"]
    if DOCKER_HOST != "unix:///var/run/docker.sock":
        cmd += ["-H", DOCKER_HOST]
    cmd += [
        "run", "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "128",
        "--memory", "1g",
        "--memory-swap", "1g",
        "--cpus", "2",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=1g",
        "-v", f"{work_dir}:/work:rw",
        # No secrets in worker env (cross-cutting rule B)
        "-e", "HOME=/tmp",
        "-e", "TMPDIR=/tmp",
        "-e", "CALIBRE_TEMP_DIR=/tmp",
        "-e", "CALIBRE_CONFIG_DIRECTORY=/tmp/calibre",
        "-e", "MPLCONFIGDIR=/tmp/mpl",
    ]
    if settings.RUNSC_RUNTIME:
        cmd += ["--runtime", settings.RUNSC_RUNTIME]
    cmd += [
        settings.CONVERTER_IMAGE,
        "python", "/app/run_convert.py",
        "/work/in.pdf", "/work/out.epub",
        "--title", title,
        "--author", author,
        "--ocr-langs", settings.OCR_LANGS,
    ]
    return cmd


async def convert_pdf(job_id: str, pdf_path: Path, title: str, author: str) -> Path:
    """Orchestrate the gVisor worker. Returns path to out.epub in ready/."""
    await job_svc.update_job(job_id, status="converting")

    async with _sem():
        out_path = pdf_path.parent / "out.epub"
        scratch_dir = pdf_path.parent
        cmd = _docker_cmd(job_id, pdf_path, title, author)

        try:
            # Run in executor so we don't block the event loop during conversion.
            await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None, _run_docker, cmd
                ),
                timeout=settings.CONVERT_TIMEOUT_S + settings.OCR_TIMEOUT_S + 30,
            )
        except asyncio.TimeoutError:
            raise RuntimeError("Converter timed out")
        except subprocess.CalledProcessError as exc:
            stderr_tail = (exc.stderr or b"")[-4000:].decode("utf-8", errors="replace")
            raise RuntimeError(f"Converter failed:\n{stderr_tail}")
        finally:
            pass  # scratch_dir cleaned by caller

        if not out_path.exists():
            raise RuntimeError("Worker exited 0 but out.epub not found")

        ready_dir = Path(settings.DATA_DIR) / "ready"
        ready_dir.mkdir(parents=True, exist_ok=True)
        dest = ready_dir / f"{job_id}.epub"
        shutil.move(str(out_path), str(dest))
        return dest


def _run_docker(cmd: list[str]) -> None:
    result = subprocess.run(
        cmd,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr
        )
