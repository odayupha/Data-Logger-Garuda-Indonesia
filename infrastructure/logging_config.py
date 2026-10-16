"""
infrastructure/logging_config.py
──────────────────────────────────
Centralized logging setup for the Flap Sensor Data Logger backend.

Call ``configure_logging()`` once at startup (before importing services).
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from config import settings


def configure_logging(
    log_level: str = settings.LOG_LEVEL,
    log_file: str | Path | None = None,
) -> None:
    """
    Configure root logger with a console handler and an optional rotating
    file handler.

    Parameters
    ----------
    log_level : str
        One of DEBUG, INFO, WARNING, ERROR, CRITICAL.
    log_file : path or None
        If provided, logs are also written to this file (10 MB rotating,
        max 3 backups).  Log file path defaults to None (console only).
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    handlers: list[logging.Handler] = []

    # Console handler (stderr)
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(
        logging.Formatter(settings.LOG_FORMAT, datefmt=settings.LOG_DATE_FORMAT)
    )
    handlers.append(console)

    # Optional rotating file handler
    if log_file is not None:
        fh = logging.handlers.RotatingFileHandler(
            str(log_file),
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=3,
            encoding="utf-8",
        )
        fh.setLevel(logging.DEBUG)  # File captures everything
        fh.setFormatter(
            logging.Formatter(settings.LOG_FORMAT, datefmt=settings.LOG_DATE_FORMAT)
        )
        handlers.append(fh)

    logging.basicConfig(level=level, handlers=handlers, force=True)
    logging.getLogger(__name__).info(
        "Logging configured: level=%s, file=%s", log_level, log_file
    )
