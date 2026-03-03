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
    """Configure logging with consistent handlers and formatting.

    Safe to call after third-party imports that may have already attached a
    StreamHandler via ``logging.basicConfig()``.  Rather than bailing out when
    handlers are already present, this function checks for each handler type
    individually so the FileHandler is always registered when requested.
    """
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Add a stream handler only when none exists yet (avoids duplicate console output).
    has_stream = any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in root_logger.handlers
    )
    if not has_stream:
        sh = stream_handler or TqdmStreamHandler(sys.stderr)
        sh.setFormatter(formatter)
        root_logger.addHandler(sh)

    # Always add the file handler when requested, unless one for the same path
    # is already registered (guards against being called twice with the same file).
    if log_file:
        target = str(Path(log_file).resolve())
        has_file = any(
            isinstance(h, logging.FileHandler)
            and str(Path(h.baseFilename).resolve()) == target
            for h in root_logger.handlers
        )
        if not has_file:
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(formatter)
            root_logger.addHandler(fh)
