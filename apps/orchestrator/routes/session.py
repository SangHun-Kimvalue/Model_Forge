"""``/session`` routes (DESIGN.md §4.2)."""

from typing import cast

from fastapi import APIRouter, HTTPException, Request, status

from apps.orchestrator.dependencies import Dependencies
from apps.orchestrator.exceptions import SessionNotFoundError
from apps.orchestrator.schemas import (
    SessionCreateRequest,
    SessionCreateResponse,
    SessionStateView,
)

router = APIRouter(prefix="/session", tags=["session"])


@router.post("", response_model=SessionCreateResponse, status_code=201)
async def create_session(
    body: SessionCreateRequest,
    request: Request,
) -> SessionCreateResponse:
    """Create a new orchestrator session."""
    deps = cast(Dependencies, request.app.state.deps)
    session = await deps.sessions.create()
    deps.logger.info(
        "session_created",
        extra={
            "session_id": session.session_id,
            "trace_id": session.trace_id,
            "client_label": body.client_label,
        },
    )
    return SessionCreateResponse(
        session_id=session.session_id,
        trace_id=session.trace_id,
        state=session.state,
        created_at=session.created_at,
    )


@router.get("/{session_id}", response_model=SessionStateView)
async def get_session(session_id: str, request: Request) -> SessionStateView:
    """Snapshot of the session's current state."""
    deps = cast(Dependencies, request.app.state.deps)
    try:
        session = await deps.sessions.get(session_id)
    except SessionNotFoundError as exc:
        deps.logger.info("session.not_found", extra={"session_id": session_id})
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return session.to_view()


__all__ = ["router"]
