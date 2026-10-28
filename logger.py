"""
logger.py
=========
Single source of truth for logging configuration.

Logs go to two places:
  * the console (so an operator running the CLI sees progress live), and
  * a rotating ``app.log`` file (so failures can be diagnosed after the fact).

Levels are used deliberately:
  INFO    - normal progress ("loaded 412 rating rows", "wrote 37 cut tasks")
  WARNING - recoverable anomalies (unmatched product, NaN row skipped, retry)
  ERROR   - aborted operation (bad schema, API gave up after all retries)
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from config import LOG_FILE

_CONFIGURED = False

_FMT = "%(asctime)s | %(levelname)-7s | %(name)-14s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _force_utf8_streams() -> None:
    """Make the Windows console emit UTF-8 so Cyrillic output never crashes.

    The legacy console defaults to cp1252; ``print``-ing Cyrillic raises
    UnicodeEncodeError. Reconfiguring to UTF-8 (with a safety 'replace') fixes
    both logging and plain prints. Guarded because some wrapped streams
    (pytest capture) lack ``reconfigure``.
    """
    import sys

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def setup_logging(verbose: bool = False) -> None:
    """Configure root logging once. Safe to call multiple times."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    _force_utf8_streams()

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    formatter = logging.Formatter(_FMT, datefmt=_DATEFMT)

    # Console handler
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(formatter)
    root.addHandler(console)

    # Rotating file handler: 2 MB x 5 files, UTF-8 so Cyrillic logs cleanly.
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # gspread / urllib3 are chatty; keep them at WARNING.
    for noisy in ("urllib3", "googleapiclient", "google", "gspread"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a module-scoped logger."""
    return logging.getLogger(name)
