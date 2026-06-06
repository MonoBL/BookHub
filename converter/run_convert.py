#!/usr/bin/env python3
"""PDF -> EPUB conversion worker.

Runs INSIDE the gVisor container. Called as:
  python run_convert.py /work/in.pdf /work/out.epub \
      --title "..." --author "..." --ocr-langs eng+por+fra+spa
"""
import argparse
import os
import resource
import subprocess
import sys
import tempfile
from pathlib import Path


def _set_rlimits() -> None:
    """Second-layer resource caps inside the container."""
    GB = 1024 ** 3
    try:
        resource.setrlimit(resource.RLIMIT_AS,    (2 * GB, 2 * GB))
        resource.setrlimit(resource.RLIMIT_FSIZE,  (1 * GB, 1 * GB))
        resource.setrlimit(resource.RLIMIT_NPROC,  (128, 128))
        resource.setrlimit(resource.RLIMIT_CPU,    (1800, 1800))
    except Exception:
        pass  # gVisor may reject some rlimits; best-effort


def _pdftotext_words(pdf: Path, page: int) -> int:
    """Count words on a single page via pdftotext."""
    try:
        r = subprocess.run(
            ["pdftotext", "-f", str(page), "-l", str(page), str(pdf), "-"],
            capture_output=True, timeout=30,
        )
        return len(r.stdout.split())
    except Exception:
        return 0


def _density_probe(pdf: Path) -> float:
    """Return average words-per-page across evenly-spaced sample pages."""
    try:
        r = subprocess.run(
            ["pdfinfo", str(pdf)], capture_output=True, timeout=10,
        )
        pages = 1
        for line in r.stdout.decode("utf-8", errors="replace").splitlines():
            if line.lower().startswith("pages:"):
                pages = max(1, int(line.split(":", 1)[1].strip()))
                break
    except Exception:
        pages = 1

    sample = max(1, min(10, pages))
    step = max(1, pages // sample)
    sample_pages = list(range(1, pages + 1, step))[:sample]

    total_words = sum(_pdftotext_words(pdf, p) for p in sample_pages)
    return total_words / len(sample_pages)


def _run_ocr(pdf: Path, out: Path, langs: str, timeout: int) -> bool:
    """Run ocrmypdf. Returns True on success."""
    try:
        proc = subprocess.Popen(
            [
                "ocrmypdf",
                "--skip-text",
                "-l", langs,
                "--output-type", "pdf",
                str(pdf), str(out),
            ],
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            stdout, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), 15)
            proc.wait()
            print("[run_convert] OCR timed out; falling back to original PDF", flush=True)
            return False

        if proc.returncode != 0:
            tail = (stdout or b"")[-2000:].decode("utf-8", errors="replace")
            print(f"[run_convert] OCR failed (rc={proc.returncode}):\n{tail}", flush=True)
            return False

        return True
    except FileNotFoundError:
        print("[run_convert] ocrmypdf not found; skipping OCR", flush=True)
        return False


def _run_ebook_convert(src: Path, dst: Path, title: str, author: str, timeout: int) -> None:
    """Run calibre ebook-convert. Raises on failure."""
    cmd = [
        "ebook-convert",
        str(src), str(dst),
        "--enable-heuristics",
        "--unwrap-factor", "0.5",
        "--title", title,
        "--authors", author,
    ]
    proc = subprocess.Popen(
        cmd,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        stdout, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), 15)
        proc.wait()
        raise RuntimeError(f"ebook-convert timed out after {timeout}s")

    tail = (stdout or b"")[-4000:].decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ebook-convert failed (rc={proc.returncode}):\n{tail}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_pdf")
    parser.add_argument("output_epub")
    parser.add_argument("--title", default="Unknown Title")
    parser.add_argument("--author", default="Unknown Author")
    parser.add_argument("--ocr-langs", default="eng+por+fra+spa")
    parser.add_argument("--ocr-timeout", type=int, default=1200)
    parser.add_argument("--convert-timeout", type=int, default=600)
    parser.add_argument("--density-threshold", type=float, default=10.0)
    args = parser.parse_args()

    _set_rlimits()

    pdf = Path(args.input_pdf)
    epub = Path(args.output_epub)

    print("[run_convert] Probing text density...", flush=True)
    density = _density_probe(pdf)
    print(f"[run_convert] Density: {density:.1f} words/page", flush=True)

    src_pdf = pdf
    if density < args.density_threshold:
        print("[run_convert] Low density - running OCR...", flush=True)
        with tempfile.NamedTemporaryFile(suffix=".pdf", dir=pdf.parent, delete=False) as f:
            ocr_out = Path(f.name)
        success = _run_ocr(pdf, ocr_out, args.ocr_langs, args.ocr_timeout)
        if success:
            src_pdf = ocr_out
        else:
            try:
                ocr_out.unlink(missing_ok=True)
            except Exception:
                pass
    else:
        print("[run_convert] Text-first PDF - skipping OCR", flush=True)

    print("[run_convert] Running ebook-convert...", flush=True)
    _run_ebook_convert(src_pdf, epub, args.title, args.author, args.convert_timeout)

    if src_pdf != pdf:
        try:
            src_pdf.unlink(missing_ok=True)
        except Exception:
            pass

    print("[run_convert] Done.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[run_convert] FATAL: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
