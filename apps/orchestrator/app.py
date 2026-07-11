"""FastAPI app factory (DESIGN.md §4.2).

The factory takes either a fully built ``Dependencies`` bundle or an
``OrchestratorSettings`` that triggers default construction. Tests
inject their own ``Dependencies`` so each test gets an isolated
in-memory store and a stub planner.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from apps.orchestrator.dependencies import (
    Dependencies,
    OrchestratorSettings,
    build_dependencies,
)
from apps.orchestrator.routes import (
    approve,
    artifacts,
    chat,
    clarify,
    intake_choice,
    session,
    ws,
)


def create_app(
    *,
    dependencies: Dependencies | None = None,
    settings: OrchestratorSettings | None = None,
) -> FastAPI:
    """Build a fresh FastAPI app bound to ``dependencies`` (or a default bundle)."""

    deps = dependencies or build_dependencies(settings)
    app = FastAPI(
        title="Model Forge Orchestrator",
        version="0.1.0",
        description=(
            "Agentic 3D-printing pipeline public API (DESIGN.md §4.2). "
            "Phase 6 — Orchestrator API skeleton."
        ),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(deps.settings.allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )
    app.state.deps = deps
    app.include_router(session.router)
    app.include_router(chat.router)
    app.include_router(clarify.router)
    app.include_router(intake_choice.router)
    app.include_router(approve.router)
    app.include_router(artifacts.router)
    app.include_router(ws.router)
    return app


__all__ = ["create_app"]
