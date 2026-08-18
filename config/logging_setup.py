"""
GitPulse - logging configuration.

Configures Python's standard `logging` module so that:

* Application errors go to an `app.log` file.
* Authentication events go to `auth.log`.
* GitHub API issues go to `github.log`.
* Code scanner results go to `scanner.log`.

Each logger also mirrors to the console so `docker logs` / `heroku logs`
work out of the box.
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Optional

# Well-known loggers used across the project. Keeping their names here
# avoids typos and makes it easy to attach the right file handler.
LOGGER_NAMES = {
    "app": "app.log",
    "auth": "auth.log",
    "github": "github.log",
    "scanner": "scanner.log",
}

# Set of loggers that have already been configured (idempotent setup).
_configured: set[str] = set()


def _build_handler(filename: str, log_dir: str, level: int) -> RotatingFileHandler:
    """Create a rotating file handler so logs never grow unbounded."""
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, filename)
    handler = RotatingFileHandler(
        path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
        )
    )
    return handler


def _configure_logger(name: str, level: int, log_dir: str) -> None:
    """Wire a named logger to its file handler plus the console."""
    if name in _configured:
        return

    logger = logging.getLogger(name)
    logger.setLevel(level)

    # File handler for persistent storage.
    if name in LOGGER_NAMES:
        logger.addHandler(_build_handler(LOGGER_NAMES[name], log_dir, level))

    # Console handler for docker logs / local terminal.
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter("%(levelname)s | %(name)s | %(message)s"))
    logger.addHandler(console)

    # Prevent duplicate log lines when handlers are re-added.
    logger.propagate = False
    _configured.add(name)


def setup_logging(log_dir: str = "logs", level: Optional[str] = None) -> None:
    """
    Configure all project loggers.

    Args:
        log_dir: Directory where log files are written.
        level:   Optional log level override (e.g. "DEBUG").
    """
    resolved_level = getattr(logging, (level or "INFO").upper(), logging.INFO)
    for name in LOGGER_NAMES:
        _configure_logger(name, resolved_level, log_dir)

    # Root logger catches anything else (e.g. Werkzeug HTTP access lines).
    root = logging.getLogger()
    root.setLevel(resolved_level)
    if not root.handlers:
        root.addHandler(logging.StreamHandler())


def get_logger(name: str) -> logging.Logger:
    """Return one of the well-known loggers (safe for unknown names)."""
    return logging.getLogger(name if name in LOGGER_NAMES else "app")
