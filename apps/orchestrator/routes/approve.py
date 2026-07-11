"""``/approve`` route (DESIGN.md §4.2, P9 human-in-the-loop)."""

import asyncio
from typing import cast

from fastapi import APIRouter, HTTPException, Request, status

from apps.orchestrator.dependencies import Dependencies
from apps.orchestrator.exceptions import (
    ApprovalGateNotFoundError,
    SessionNotFoundError,
    SessionStateError,
)
from apps.orchestrator.graph import (
    ApprovalExecutionPlan,
    continue_approval_execution,
    start_approval_resolution,
)
from apps.orchestrator.schemas import (
    ApprovalDecision,
    ApproveJobRequest,
    ApproveRequest,
    ApproveResponse,
)

router = APIRouter(tags=["approve"])


@router.post("/approve", response_model=ApproveResponse)
async def approve(body: ApproveRequest, request: Request) -> ApproveResponse:
    """Resolve a pending human-approval gate."""
    return await _approve_gate(
        request=request,
        session_id=body.session_id,
        gate_id=body.gate_id,
        decision=body.decision,
        comments=body.comments,
    )


@router.post("/approve/{job_id}", response_model=ApproveResponse)
async def approve_job(
    job_id: str, body: ApproveJobRequest, request: Request
) -> ApproveResponse:
    """Resolve a pending approval job.

    Phase 6 maps ``job_id`` to the in-memory approval gate id; durable job ids
    can be introduced behind this route in a later phase.
    """
    return await _approve_gate(
        request=request,
        session_id=body.session_id,
        gate_id=job_id,
        decision=body.decision,
        comments=body.comments,
    )


async def _approve_gate(
    *,
    request: Request,
    session_id: str,
    gate_id: str,
    decision: ApprovalDecision,
    comments: str | None,
) -> ApproveResponse:
    deps = cast(Dependencies, request.app.state.deps)
    try:
        session = await deps.sessions.get(session_id)
    except SessionNotFoundError as exc:
        deps.logger.info("session.not_found", extra={"session_id": session_id})
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    lock = deps.sessions.lock_for(session_id)
    async with lock:
        try:
            plan = await start_approval_resolution(
                deps,
                session,
                gate_id,
                decision,
                comments,
            )
        except ApprovalGateNotFoundError as exc:
            deps.logger.info(
                "approval.gate_not_found",
                extra={"session_id": session_id, "gate_id": gate_id},
            )
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except SessionStateError as exc:
            deps.logger.warning(
                "session.state_conflict",
                extra={"session_id": session_id, "state": str(session.state)},
            )
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

        if plan.should_execute:
            _schedule_approval_execution(deps, session_id, plan)

        return ApproveResponse(
            session_id=session.session_id,
            gate_id=gate_id,
            state=session.state,
            decision=decision,
        )


def _schedule_approval_execution(
    deps: Dependencies,
    session_id: str,
    plan: ApprovalExecutionPlan,
) -> None:
    task = asyncio.create_task(_finish_approval_execution(deps, session_id, plan))

    def _log_unhandled_failure(done: asyncio.Task[None]) -> None:
        if done.cancelled():
            deps.logger.warning(
                "approval.background_cancelled",
                extra={"session_id": session_id},
            )
            return
        if exc := done.exception():
            deps.logger.error(
                "approval.background_unhandled",
                extra={"session_id": session_id},
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    task.add_done_callback(_log_unhandled_failure)


async def _finish_approval_execution(
    deps: Dependencies,
    session_id: str,
    plan: ApprovalExecutionPlan,
) -> None:
    session = await deps.sessions.get(session_id)
    lock = deps.sessions.lock_for(session_id)
    async with lock:
        await continue_approval_execution(
            deps,
            session,
            plan.approved_subtasks,
            approval_gate_id=plan.gate_id,
            plan_snapshot_id=plan.plan_snapshot_id,
        )


__all__ = ["router"]
