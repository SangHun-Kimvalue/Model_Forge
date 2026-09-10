"""``/clarify`` route for Phase 11C-4 ask-user gates."""

import asyncio
from typing import cast

from fastapi import APIRouter, HTTPException, Request, status

from apps.orchestrator.dependencies import Dependencies
from apps.orchestrator.exceptions import (
    ClarificationNotFoundError,
    SessionNotFoundError,
    SessionStateError,
)
from apps.orchestrator.graph import (
    ClarificationExecutionPlan,
    continue_approval_execution,
    start_clarification_resolution,
)
from apps.orchestrator.schemas import ClarifyRequest, ClarifyResponse
from apps.orchestrator.sessions import pending_clarification_view

router = APIRouter(tags=["clarify"])


@router.post("/clarify", response_model=ClarifyResponse)
async def clarify(body: ClarifyRequest, request: Request) -> ClarifyResponse:
    """Resolve a pending clarification and resume the approved pipeline."""
    deps = cast(Dependencies, request.app.state.deps)
    try:
        session = await deps.sessions.get(body.session_id)
    except SessionNotFoundError as exc:
        deps.logger.info("session.not_found", extra={"session_id": body.session_id})
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    lock = deps.sessions.lock_for(body.session_id)
    async with lock:
        try:
            plan = await start_clarification_resolution(
                deps,
                session,
                body.clarification_id,
                body.answers,
            )
        except ClarificationNotFoundError as exc:
            deps.logger.info(
                "clarification.not_found",
                extra={
                    "session_id": body.session_id,
                    "clarification_id": body.clarification_id,
                },
            )
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except SessionStateError as exc:
            deps.logger.warning(
                "clarification.state_conflict",
                extra={"session_id": body.session_id, "state": str(session.state)},
            )
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

        if plan.should_execute:
            _schedule_clarification_execution(deps, body.session_id, plan)

        return ClarifyResponse(
            session_id=session.session_id,
            clarification_id=body.clarification_id,
            accepted=True,
            state=session.state,
            route_result=session.latest_route_result,
            pending_clarification=pending_clarification_view(session),
        )


def _schedule_clarification_execution(
    deps: Dependencies,
    session_id: str,
    plan: ClarificationExecutionPlan,
) -> None:
    task = asyncio.create_task(_finish_clarification_execution(deps, session_id, plan))

    def _log_unhandled_failure(done: asyncio.Task[None]) -> None:
        if done.cancelled():
            deps.logger.warning(
                "clarification.background_cancelled",
                extra={"session_id": session_id},
            )
            return
        if exc := done.exception():
            deps.logger.error(
                "clarification.background_unhandled",
                extra={"session_id": session_id},
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    task.add_done_callback(_log_unhandled_failure)


async def _finish_clarification_execution(
    deps: Dependencies,
    session_id: str,
    plan: ClarificationExecutionPlan,
) -> None:
    session = await deps.sessions.get(session_id)
    lock = deps.sessions.lock_for(session_id)
    async with lock:
        await continue_approval_execution(
            deps,
            session,
            plan.resume_subtasks,
            approval_gate_id=plan.approval_gate_id,
            plan_snapshot_id=plan.plan_snapshot_id,
        )


__all__ = ["router"]
