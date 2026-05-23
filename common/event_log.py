import json
import logging
import sys
import threading
import time
import traceback
from logging.handlers import RotatingFileHandler

_logger = None
_ctx = threading.local()


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


def _current_context() -> dict:
    return getattr(_ctx, "data", None) or {}


def bind(**fields):
    """Bind fields (request_id, user_id, ...) to the current thread.

    All subsequent log()/log_exception() calls on this thread auto-merge these
    fields, so downstream code does not need to thread request_id manually.
    Call unbind() at the end of the request to clear.
    """
    data = getattr(_ctx, "data", None)
    if data is None:
        data = {}
        _ctx.data = data
    data.update(fields)


def unbind():
    """Clear all bound fields from the current thread."""
    _ctx.data = None


def log(event: str, **fields):
    """Write one structured JSON line to events.log. Never raises.

    Always tags `level` (default "info") so Promtail can promote it to a label
    and queries like {level="error"} work without parsing JSON. Callers can
    override by passing level="warn" etc.
    """
    if _logger is None:
        return
    try:
        merged = {"level": "info", **_current_context(), **fields}
        record = {"event": event, "ts": time.time(), **merged}
        _logger.info(json.dumps(record, ensure_ascii=False, default=str))
    except Exception:
        pass


def log_exception(event: str, exc: BaseException, **fields):
    """Log an exception event with auto-filled error_type/error_msg/stack_trace.
    Always tagged level="error" so {level="error"} catches every exception."""
    try:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    except Exception:
        tb = ""
    fields.setdefault("level", "error")
    log(
        event,
        error_type=type(exc).__name__,
        error_msg=str(exc),
        stack_trace=tb,
        **fields,
    )


_excepthooks_installed = False


def install_excepthooks():
    """Install global hooks so uncaught exceptions land in events.log.

    Covers both the main thread (sys.excepthook) and worker threads
    (threading.excepthook, Python 3.8+). Safe to call multiple times.
    """
    global _excepthooks_installed
    if _excepthooks_installed:
        return

    _prev_sys_hook = sys.excepthook

    def _sys_hook(exc_type, exc, tb):
        try:
            if exc is None:
                exc = exc_type() if isinstance(exc_type, type) else Exception(str(exc_type))
            log_exception("unhandled_exception", exc, source="sys.excepthook")
        except Exception:
            pass
        try:
            _prev_sys_hook(exc_type, exc, tb)
        except Exception:
            pass

    sys.excepthook = _sys_hook

    if hasattr(threading, "excepthook"):
        _prev_thread_hook = threading.excepthook

        def _thread_hook(args):
            try:
                exc = args.exc_value
                thread_name = getattr(args.thread, "name", "")
                if exc is None:
                    log(
                        "unhandled_exception",
                        level="error",
                        source="threading.excepthook",
                        thread_name=thread_name,
                        error_type=getattr(args.exc_type, "__name__", "Unknown"),
                        error_msg="",
                    )
                else:
                    log_exception(
                        "unhandled_exception",
                        exc,
                        source="threading.excepthook",
                        thread_name=thread_name,
                    )
            except Exception:
                pass
            try:
                _prev_thread_hook(args)
            except Exception:
                pass

        threading.excepthook = _thread_hook

    _excepthooks_installed = True
