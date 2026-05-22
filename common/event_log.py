import json
import logging
import time
from logging.handlers import RotatingFileHandler

_logger = None


def _init():
    global _logger
    try:
        handler = RotatingFileHandler(
            "events.log",
            maxBytes=100 * 1024 * 1024,
            backupCount=10,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("event_log")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.propagate = False
        _logger = logger
    except Exception:
        _logger = None


_init()


def log(event: str, **fields):
    """Write one structured JSON line to events.log. Never raises."""
    if _logger is None:
        return
    try:
        record = {"event": event, "ts": time.time(), **fields}
        _logger.info(json.dumps(record, ensure_ascii=False, default=str))
    except Exception:
        pass
