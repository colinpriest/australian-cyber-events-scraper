from __future__ import annotations

import logging
from typing import Optional


def setup_logging(
    log_file: Optional[str] = None,
    level: int = logging.INFO,
    stream_handler: Optional[logging.Handler] = None,
) -> None:
    """Configure logging with consistent handlers and formatting."""

    root_logger = logging.getLogger()
    if root_logger.handlers:
        return

    handlers = [stream_handler or logging.StreamHandler()]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )
