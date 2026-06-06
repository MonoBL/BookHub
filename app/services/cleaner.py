# TTL cleaner - implemented in M4.
import asyncio
import logging

logger = logging.getLogger("bookhub")


async def run_cleaner() -> None:
    """Background task: sweeps quarantine/ready dirs and prunes DB. See BUILD.md §7.6."""
    while True:
        await asyncio.sleep(300)  # every 5 minutes
