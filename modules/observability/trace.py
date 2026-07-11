"""Trace context shared by every module on the request path.

The Model Forge orchestrator binds ``trace_id`` / ``session_id`` / ``job_id`` at
the request boundary; every downstream module reads the active values through
this module so logs and Langfuse spans line up (DESIGN.md §P7).

Contextvars are used so async tasks **inherit a copy** of the parent context
when spawned via ``asyncio.create_task()`` or ``loop.run_in_executor()``.
This means:
  - Parent → child: bindings visible at spawn-time are readable in the child.
  - Child → parent: changes made **inside** the child's ``bind_trace`` are
    **NOT** propagated back to the parent. Each task has its own isolated copy.

This is intentional (read-only inheritance from the parent perspective).
The orchestrator should bind all IDs before spawning tasks; tasks must not
attempt to propagate new bindings upward.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

_trace_id: ContextVar[str | None] = ContextVar("model_forge_trace_id", default=None)
_session_id: ContextVar[str | None] = ContextVar("model_forge_session_id", default=None)
_job_id: ContextVar[str | None] = ContextVar("model_forge_job_id", default=None)


@dataclass(frozen=True)
class TraceContext:
    """Snapshot of the active trace binding."""

    trace_id: str | None = None
    session_id: str | None = None
    job_id: str | None = None

    def as_log_fields(self) -> dict[str, str]:
        """Return only the populated fields, suitable for JSON log extras."""
        fields: dict[str, str] = {}
        if self.trace_id is not None:
            fields["trace_id"] = self.trace_id
        if self.session_id is not None:
            fields["session_id"] = self.session_id
        if self.job_id is not None:
            fields["job_id"] = self.job_id
        return fields


def current_trace() -> TraceContext:
    """Return a snapshot of the currently bound trace context."""
    return TraceContext(
        trace_id=_trace_id.get(),
        session_id=_session_id.get(),
        job_id=_job_id.get(),
    )


def current_trace_fields() -> dict[str, str]:
    """Convenience accessor: only the populated trace fields as a dict."""
    return current_trace().as_log_fields()


@contextmanager
def bind_trace(
    *,
    trace_id: str | None = None,
    session_id: str | None = None,
    job_id: str | None = None,
) -> Iterator[TraceContext]:
    """Bind trace ids for the duration of the ``with`` block.

    Unspecified ids inherit the existing binding rather than being cleared,
    so nested binds can extend a parent context (e.g. orchestrator binds
    session_id, a job worker adds job_id) without clobbering siblings.
    """
    tokens = []
    if trace_id is not None:
        tokens.append(_trace_id.set(trace_id))
    if session_id is not None:
        tokens.append(_session_id.set(session_id))
    if job_id is not None:
        tokens.append(_job_id.set(job_id))
    try:
        yield current_trace()
    finally:
        for token in reversed(tokens):
            token.var.reset(token)
