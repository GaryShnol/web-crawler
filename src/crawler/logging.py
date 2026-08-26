"""JSON-lines logging to stdout, with contextvar-bound fields (url, worker_id, ...)."""

import contextlib
import contextvars
import json
import logging
import sys
from collections.abc import Iterator

_context: contextvars.ContextVar[dict[str, object] | None] = contextvars.ContextVar(
    "log_context", default=None
)


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.context = _context.get() or {}
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **getattr(record, "context", {}),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str) -> None:
    """Set up JSON-lines logging to stdout. Call once, at process start."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    handler.addFilter(_ContextFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


@contextlib.contextmanager
def bind(**fields: object) -> Iterator[None]:
    """Add fields to every log record emitted while the context manager is open.

    A worker wraps its per-URL loop in this (e.g. `with bind(url=url, worker_id=n):`)
    so every log line from that iteration carries the context, with no logger
    threaded through the call stack to do it.
    """
    token = _context.set({**(_context.get() or {}), **fields})
    try:
        yield
    finally:
        _context.reset(token)
