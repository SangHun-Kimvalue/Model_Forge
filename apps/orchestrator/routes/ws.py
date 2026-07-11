"""``WS /ws/{session_id}`` subscriber (DESIGN.md §4.2).

The orchestrator does not push every event synchronously to clients —
events flow through a per-session queue (``SessionStore.subscribe``).
The WS handler simply forwards the queue to the wire as JSON.

Clients should reconnect with a fresh subscription on disconnect; we
do not replay missed events (R10 keeps that explicit instead of
faking durable delivery on top of an in-memory store).
"""

import asyncio
from typing import cast

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from apps.orchestrator.dependencies import Dependencies
from apps.orchestrator.exceptions import SessionNotFoundError

router = APIRouter(tags=["ws"])


@router.websocket("/ws/{session_id}")
async def session_events(websocket: WebSocket, session_id: str) -> None:
    """Stream ``SessionEvent`` JSON payloads for the lifetime of the WS."""
    deps = cast(Dependencies, websocket.app.state.deps)
    try:
        await deps.sessions.get(session_id)
    except SessionNotFoundError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    queue = await deps.sessions.subscribe(session_id)
    try:
        while True:
            event = await queue.get()
            await websocket.send_text(event.model_dump_json())
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        raise
    finally:
        await deps.sessions.unsubscribe(session_id, queue)


__all__ = ["router"]
