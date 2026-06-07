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


def _docker_cmd(
    job_id: str,
    pdf_path: Path,
    title: str,
    author: str,
    convert_timeout: int,
    ocr_timeout: int,
    density_threshold: float = 10.0,
) -> list[str]:
    work_dir = str(pdf_path.parent)

    # Translate container-side work_dir to the host path the daemon can resolve.
    if settings.HOST_DATA_DIR:
        try:
            rel = Path(work_dir).relative_to(settings.DATA_DIR)
            host_src = str(Path(settings.HOST_DATA_DIR) / rel)
        except ValueError:
            log.warning("work_dir %s outside DATA_DIR %s; using as-is", work_dir, settings.DATA_DIR)
            host_src = work_dir
    else:
        host_src = work_dir

    container_name = f"bookhub-conv-{job_id}"
    cmd = ["docker"]
    if DOCKER_HOST != "unix:///var/run/docker.sock":
        cmd += ["-H", DOCKER_HOST]
    cmd += [
        "run", "--rm",
        "--name", container_name,
        "--network=none",
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "128",
        "--memory", "1g",
        "--memory-swap", "1g",
        "--cpus", "2",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=1g",
        "-v", f"{host_src}:/work:rw",
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
        # The image ENTRYPOINT is ["python", "/app/run_convert.py"], so pass
        # only the script's arguments here (do not repeat the interpreter).
        settings.CONVERTER_IMAGE,
        "/work/in.pdf", "/work/out.epub",
        "--title", title,
        "--author", author,
        "--ocr-langs", settings.OCR_LANGS,
        "--convert-timeout", str(convert_timeout),
        "--ocr-timeout", str(ocr_timeout),
        # density_threshold=0 disables OCR (comic/image mode): OCR mangles art.
        "--density-threshold", str(density_threshold),
    ]
    return cmd


def _kill_container(name: str) -> None:
    """Kill a named container (best-effort, ignores all errors)."""
    try:
        kill_cmd = ["docker"]
        if DOCKER_HOST != "unix:///var/run/docker.sock":
            kill_cmd += ["-H", DOCKER_HOST]
        kill_cmd += ["kill", name]
        subprocess.run(kill_cmd, timeout=10, capture_output=True)
    except Exception:
        pass


async def convert_pdf(
    job_id: str, pdf_path: Path, title: str, author: str, comic: bool = False
) -> Path:
    """Orchestrate the gVisor worker. Returns path to out.epub in ready/.

    comic=True: image/comic PDF. OCR is SKIPPED (it mangles color art), images
    are preserved as-is, and the convert timeout is raised for many-page books."""
    await job_svc.update_job(job_id, status="converting")

    convert_timeout = settings.COMIC_CONVERT_TIMEOUT_S if comic else settings.CONVERT_TIMEOUT_S
    ocr_timeout = settings.COMIC_OCR_TIMEOUT_S if comic else settings.OCR_TIMEOUT_S
    # Comic mode disables OCR by setting the density threshold to 0 (no page
    # ever falls below it). Text/scanned PDFs keep the default auto-OCR gate.
    density_threshold = 0.0 if comic else 10.0

    async with _sem():
        out_path = pdf_path.parent / "out.epub"
        scratch_dir = pdf_path.parent
        container_name = f"bookhub-conv-{job_id}"
        cmd = _docker_cmd(
            job_id, pdf_path, title, author,
            convert_timeout, ocr_timeout, density_threshold,
        )

        try:
            # Run in executor so we don't block the event loop during conversion.
            await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None, _run_docker, cmd
                ),
                timeout=convert_timeout + ocr_timeout + 60,
            )
        except asyncio.TimeoutError:
            _kill_container(container_name)
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
