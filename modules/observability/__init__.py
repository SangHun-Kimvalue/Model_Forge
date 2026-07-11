"""Structured logging + trace propagation primitives (DESIGN.md §P7).

Public surface:
    - JsonFormatter — stdlib-compatible JSON log formatter.
    - configure_logging — install JsonFormatter on the root logger.
    - bind_trace / current_trace_fields — contextvar-backed trace context.
    - mock_selected_extra — canonical ``extra=`` payload for factory WARN
      events so LLM, Organic Generator, and Agents emit the same schema.
"""

from modules.observability.events import (
    MOCK_SELECTED_EVENT_SUFFIX,
    mock_selected_event,
    mock_selected_extra,
)
from modules.observability.logging_config import JsonFormatter, configure_logging
from modules.observability.trace import (
    TraceContext,
    bind_trace,
    current_trace_fields,
)

__all__ = [
    "JsonFormatter",
    "MOCK_SELECTED_EVENT_SUFFIX",
    "TraceContext",
    "bind_trace",
    "configure_logging",
    "current_trace_fields",
    "mock_selected_event",
    "mock_selected_extra",
]
