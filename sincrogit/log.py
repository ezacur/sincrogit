"""Logging setup: to a rotating file + console.

The file is essential because in production the daemon runs with `pythonw.exe`
(no console). See §7 and §9 of DESIGN.md.
"""

import logging
import os
from logging.handlers import RotatingFileHandler

_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _resolve_level(level) -> int:
    """A logging level from either a name ('DEBUG'/'warning') or a number (10).
    YAML parses `level: 10` as an int, which str().upper() would turn into the
    bogus attribute '10' and silently fall back to INFO — so handle ints first."""
    if isinstance(level, bool):   # bool is an int subclass; not a real level
        return logging.INFO
    if isinstance(level, int):
        return level
    return getattr(logging, str(level).strip().upper(), logging.INFO)


def setup_logging(log_file: str | None, level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("sincrogit")
    logger.setLevel(_resolve_level(level))
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter(_FORMAT, _DATEFMT)

    if log_file:
        log_dir = os.path.dirname(os.path.abspath(log_file))
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        fh = RotatingFileHandler(
            log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger
