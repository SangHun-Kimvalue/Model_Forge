"""Artifact download + manifest routes (ADR-0005)."""

from typing import cast

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, Response

from apps.orchestrator.dependencies import Dependencies
from modules.artifacts import (
    ArtifactPathError,
    ManifestSchemaError,
    get_artifact_root,
    read_manifest,
    resolve_manifest_artifact_path,
)

router = APIRouter(tags=["artifacts"])


@router.get(
    "/sessions/{session_id}/subtasks/{subtask_id}/manifest",
    summary="Read a subtask's artifact manifest (ADR-0005).",
)
async def get_subtask_manifest(
    session_id: str,
    subtask_id: str,
    request: Request,
) -> Response:
    _ = cast(Dependencies, request.app.state.deps)  # ensure app is initialised
    try:
        manifest = read_manifest(session_id, subtask_id, root=get_artifact_root())
    except ManifestSchemaError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ArtifactPathError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return Response(
        content=manifest.model_dump_json(),
        media_type="application/json",
    )


@router.get(
    "/artifacts/{session_id}/{subtask_id}/{filename:path}",
    summary="Download a single artifact file. Path-traversal is rejected.",
)
async def download_artifact(
    session_id: str,
    subtask_id: str,
    filename: str,
    request: Request,
) -> FileResponse:
    _ = cast(Dependencies, request.app.state.deps)
    try:
        path = resolve_manifest_artifact_path(
            session_id, subtask_id, filename, root=get_artifact_root()
        )
    except (ArtifactPathError, ManifestSchemaError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    return FileResponse(path)


__all__ = ["router"]
