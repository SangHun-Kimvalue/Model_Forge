"""JSON logging formatter (DESIGN.md §P7).

The formatter emits one JSON object per log record so downstream collectors
(Loki / Cloud Logging / Langfuse export) can index by ``event``, ``trace_id``,
etc. without parsing free-form strings.

Reserved stdlib LogRecord attributes are not duplicated into the JSON body;
everything passed via ``logger.warning(..., extra={...})`` lands at the top
level so tests can assert ``payload["component"] == "planner_agent"`` etc.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

_RESERVED_LOG_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class JsonFormatter(logging.Formatter):
    """Emit each log record as a single JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED_LOG_RECORD_ATTRS or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=_json_default, ensure_ascii=False)


def configure_logging(
    level: int = logging.INFO,
    *,
    logger: logging.Logger | None = None,
) -> logging.Handler:
    """Install JsonFormatter on a stderr StreamHandler.

    Returns the installed handler so callers (tests, apps) can detach it
    again. Existing handlers on the target logger are removed to keep one
    canonical JSON sink per process.

    Warning:
        When ``logger`` is ``None`` the **root logger** is targeted. This
        removes all root-logger handlers, including pytest's log-capture
        handler. Always pass an explicit named logger in tests and production
        entry points to avoid unintended side-effects.
    """
    target = logger or logging.getLogger()
    for existing in list(target.handlers):
        target.removeHandler(existing)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    target.addHandler(handler)
    target.setLevel(level)
    return handler


def _json_default(value: Any) -> str:
    return str(value)
