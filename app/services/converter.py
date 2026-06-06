# Converter orchestrator - implemented in M6.
# Launches the gVisor-sandboxed worker container via docker-socket-proxy.
# LOCAL DEV: when RUNSC_RUNTIME="" in .env, omits --runtime flag (plain docker run).
from pathlib import Path


async def convert_pdf(job_id: str, pdf_path: Path, title: str, author: str) -> Path:
    raise NotImplementedError("Converter not yet implemented")
