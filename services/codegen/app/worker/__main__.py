"""``python -m app.worker`` — run the self-hosted environment worker."""

from __future__ import annotations

import asyncio
import logging
import os

from app.worker.environment_worker import run_worker

logging.basicConfig(level=os.getenv("LOG_LEVEL", "info").upper())


def main() -> None:
    asyncio.run(run_worker(os.getenv("CODEGEN_WORKDIR", "/workspace")))


if __name__ == "__main__":
    main()
