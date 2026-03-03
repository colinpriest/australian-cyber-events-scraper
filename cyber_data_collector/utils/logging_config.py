from __future__ import annotations

import logging
import sys
from typing import Optional

try:
    from tqdm import tqdm as _tqdm
except ImportError:  # pragma: no cover
    _tqdm = None  # type: ignore[assignment]


class TqdmStreamHandler(logging.StreamHandler):
    """Stream handler that writes through tqdm.write() to avoid corrupting progress bars.

    When a tqdm progress bar is active, tqdm.write() clears the bar, prints the
    message, and redraws the bar.  When no bar is active it falls back to a
    normal stream write so there is no overhead.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            if _tqdm is not None:
                _tqdm.write(msg, file=self.stream)
            else:
                self.stream.write(msg + self.terminator)
                self.flush()
        except Exception:
            self.handleError(record)


def setup_logging(
    log_file: Optional[str] = None,
    level: int = logging.INFO,
    stream_handler: Optional[logging.Handler] = None,
) -> None:
    """Configure logging with consistent handlers and formatting."""

    root_logger = logging.getLogger()
    if root_logger.handlers:
        logging.debug("Logging already configured, skipping setup_logging")
        return

    handlers = [stream_handler or TqdmStreamHandler(sys.stderr)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )
