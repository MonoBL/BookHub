# Format verify + VirusTotal scanner - implemented in M4.
from pathlib import Path


async def verify_and_scan(job_id: str, path: Path, ext: str) -> dict:
    raise NotImplementedError("Scanner not yet implemented")
