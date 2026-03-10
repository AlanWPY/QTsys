"""QTsys logging configuration."""
import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

LOG_FILE = os.path.join(LOG_DIR, "qtsys.log")
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5


def _parse_level(value: Optional[object], default: int) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    name = str(value).strip().upper()
    return getattr(logging, name, default)


def setup_logging(level: Optional[int] = None) -> None:
    """Initialize application logging.

    Defaults:
    - file logs keep INFO details
    - console shows WARNING and above only
    - noisy framework loggers are downgraded to WARNING
    Environment overrides:
    - QT_LOG_LEVEL
    - QT_LOG_FILE_LEVEL
    - QT_LOG_CONSOLE_LEVEL
    """
    root_level = _parse_level(level if level is not None else os.getenv("QT_LOG_LEVEL"), logging.INFO)
    file_level = _parse_level(os.getenv("QT_LOG_FILE_LEVEL"), root_level)
    console_level = _parse_level(os.getenv("QT_LOG_CONSOLE_LEVEL"), logging.WARNING)

    root = logging.getLogger()
    root.setLevel(root_level)

    file_handler = next((h for h in root.handlers if isinstance(h, RotatingFileHandler)), None)
    stream_handler = next(
        (h for h in root.handlers if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)),
        None,
    )

    if file_handler is None:
        file_handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
        root.addHandler(file_handler)
    file_handler.setLevel(file_level)

    if stream_handler is None:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
        root.addHandler(stream_handler)
    stream_handler.setLevel(console_level)

    for logger_name in [
        "httpx",
        "httpcore",
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "fastapi",
        "starlette",
        "sqlalchemy.engine",
    ]:
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
