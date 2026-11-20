from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger


def setup_logging(log_dir: Path | None = None) -> None:
    logger.remove()
    logger.add(sys.stderr, level="INFO", enqueue=True)
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        logger.add(
            log_dir / "hunter.log",
            rotation="5 MB",
            retention=8,
            level="DEBUG",
            enqueue=True,
        )
