"""``/intake-choice`` route for Phase 12V asset-intake user actions."""

from typing import cast

from fastapi import APIRouter, HTTPException, Request, status

from apps.orchestrator.dependencies import Dependencies
from apps.orchestrator.exceptions import SessionNotFoundError, SessionStateError
from apps.orchestrator.graph import record_intake_choice, record_intake_next_step
from apps.orchestrator.runtime_asset_execution import execute_selected_runtime_asset
from apps.orchestrator.schemas import (
    IntakeChoiceRequest,
    IntakeChoiceResponse,
    IntakeNextStepRequest,
    IntakeNextStepView,
    RuntimeAssetExecutionRequest,
    RuntimeAssetExecutionResponse,
)

router = APIRouter(tags=["intake-choice"])


@router.post("/intake-choice", response_model=IntakeChoiceResponse)
async def intake_choice(
    body: IntakeChoiceRequest,
    request: Request,
) -> IntakeChoiceResponse:
    """Record a user choice for the latest natural-language intake route."""
    deps = cast(Dependencies, request.app.state.deps)
    try:
        session = await deps.sessions.get(body.session_id)
    except SessionNotFoundError as exc:
        deps.logger.info("session.not_found", extra={"session_id": body.session_id})
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    lock = deps.sessions.lock_for(body.session_id)
    async with lock:
        try:
            return await record_intake_choice(deps, session, body)
        except SessionStateError as exc:
            deps.logger.warning(
                "intake_choice.state_conflict",
                extra={"session_id": body.session_id, "state": str(session.state)},
            )
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/intake-next-step", response_model=IntakeNextStepView)
async def intake_next_step(
    body: IntakeNextStepRequest,
    request: Request,
) -> IntakeNextStepView:
    """Open the next safe boundary for a recorded intake choice."""
    deps = cast(Dependencies, request.app.state.deps)
    try:
        session = await deps.sessions.get(body.session_id)
    except SessionNotFoundError as exc:
        deps.logger.info("session.not_found", extra={"session_id": body.session_id})
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    lock = deps.sessions.lock_for(body.session_id)
    async with lock:
        try:
            return await record_intake_next_step(deps, session, body)
        except SessionStateError as exc:
            deps.logger.warning(
                "intake_next_step.state_conflict",
                extra={"session_id": body.session_id, "state": str(session.state)},
            )
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/runtime-asset-execution", response_model=RuntimeAssetExecutionResponse)
async def runtime_asset_execution(
    body: RuntimeAssetExecutionRequest,
    request: Request,
) -> RuntimeAssetExecutionResponse:
    """Explicitly execute a selected curated runtime asset.

    ``/intake-choice`` and ``/intake-next-step`` stay non-executing boundaries;
    this route is the opt-in bridge that can run CAD/STL/Orca.
    """
    deps = cast(Dependencies, request.app.state.deps)
    try:
        session = await deps.sessions.get(body.session_id)
    except SessionNotFoundError as exc:
        deps.logger.info("session.not_found", extra={"session_id": body.session_id})
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    lock = deps.sessions.lock_for(body.session_id)
    async with lock:
        try:
            return await execute_selected_runtime_asset(deps, session, body)
        except SessionStateError as exc:
            deps.logger.warning(
                "runtime_asset_execution.state_conflict",
                extra={"session_id": body.session_id, "state": str(session.state)},
            )
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


__all__ = ["router"]
