"""``/chat`` route (DESIGN.md §4.2)."""

from typing import cast

from fastapi import APIRouter, HTTPException, Request, status

from apps.orchestrator.dependencies import Dependencies
from apps.orchestrator.exceptions import (
    OrchestratorError,
    SessionNotFoundError,
    SessionStateError,
)
from apps.orchestrator.graph import run_chat_turn
from apps.orchestrator.schemas import ChatRequest, ChatResponse
from modules.agents.planner.exceptions import PlannerAgentError

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, request: Request) -> ChatResponse:
    """Run a single planning turn against the orchestrator graph."""
    deps = cast(Dependencies, request.app.state.deps)
    try:
        session = await deps.sessions.get(body.session_id)
    except SessionNotFoundError as exc:
        deps.logger.info("session.not_found", extra={"session_id": body.session_id})
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    lock = deps.sessions.lock_for(body.session_id)
    async with lock:
        try:
            return await run_chat_turn(deps, session, body.message)
        except SessionStateError as exc:
            deps.logger.warning(
                "session.state_conflict",
                extra={"session_id": body.session_id, "state": str(session.state)},
            )
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except PlannerAgentError as exc:
            # P8 — graph already marked the session FAILED and emitted ERROR.
            deps.logger.exception(
                "planner.agent_error", extra={"session_id": body.session_id}
            )
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        except OrchestratorError as exc:  # pragma: no cover — defensive
            deps.logger.exception(
                "orchestrator.unexpected_error", extra={"session_id": body.session_id}
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
            ) from exc


__all__ = ["router"]
