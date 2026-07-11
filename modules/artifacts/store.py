"""On-disk artifact manifest writer/reader (ADR-0005).

Layout (default ``ARTIFACT_ROOT=.model_forge/artifacts``)::

    <ARTIFACT_ROOT>/<session_id>/<subtask_id>/
        cad-result.stl
        sliced.gcode
        manifest.json

Every helper here treats ``ARTIFACT_ROOT/<session>/<subtask>`` as the
session/subtask sandbox. Paths that resolve outside it are rejected with
:class:`ArtifactPathError` (ADR-0005 invariant #1).
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from modules.artifacts.exceptions import ArtifactPathError, ManifestSchemaError
from modules.artifacts.mime import mime_for_kind, mime_for_path
from modules.artifacts.schemas import (
    MANIFEST_SCHEMA_VERSION,
    ArtifactKind,
    ArtifactRef,
    ArtifactStatus,
    ManifestError,
    ManifestStatus,
    SubtaskArtifactManifest,
)

DEFAULT_ARTIFACT_ROOT = Path(".model_forge") / "artifacts"
MANIFEST_FILENAME = "manifest.json"
_RELATIVE_URI_PREFIX = "artifacts"
_HASH_CHUNK = 1024 * 1024


def get_artifact_root(environ: dict[str, str] | None = None) -> Path:
    """Return the configured artifact root.

    Reads ``ARTIFACT_ROOT`` from ``environ`` (defaulting to ``os.environ``).
    """
    source = environ if environ is not None else os.environ
    raw = source.get("ARTIFACT_ROOT")
    return Path(raw).expanduser() if raw else DEFAULT_ARTIFACT_ROOT


def subtask_dir(root: Path, session_id: str, subtask_id: str) -> Path:
    """Return ``<root>/<session_id>/<subtask_id>``, creating it if absent."""
    d = _subtask_dir_path(root, session_id, subtask_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _subtask_dir_path(root: Path, session_id: str, subtask_id: str) -> Path:
    """Return ``<root>/<session_id>/<subtask_id>`` without touching disk."""
    _assert_safe_segment(session_id, "session_id")
    _assert_safe_segment(subtask_id, "subtask_id")
    return root / session_id / subtask_id


def _assert_safe_segment(segment: str, name: str) -> None:
    if not segment or segment in (".", "..") or "/" in segment or "\\" in segment:
        raise ArtifactPathError(
            f"Unsafe path segment for {name}: {segment!r}"
        )


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        while True:
            chunk = fp.read(_HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _relative_uri(root: Path, abs_path: Path, session_id: str, subtask_id: str) -> str:
    """Compute the public ``artifacts/<session>/<subtask>/<file>`` URI.

    Raises :class:`ArtifactPathError` if the artifact lives outside the
    expected subtask directory.
    """
    sandbox = (root / session_id / subtask_id).resolve()
    abs_resolved = abs_path.resolve()
    try:
        rel = abs_resolved.relative_to(sandbox)
    except ValueError as exc:
        raise ArtifactPathError(
            f"Artifact path {abs_path} is outside the subtask sandbox {sandbox}."
        ) from exc
    parts = (_RELATIVE_URI_PREFIX, session_id, subtask_id, *rel.parts)
    return "/".join(parts)


def build_artifact_ref(
    *,
    kind: ArtifactKind,
    path: Path,
    session_id: str,
    subtask_id: str,
    producer_adapter: str,
    producer_version: str | None = None,
    root: Path | None = None,
    mime: str | None = None,
    metadata: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> ArtifactRef:
    """Build an :class:`ArtifactRef` from a file on disk.

    Computes size and sha256 by reading the file. Rejects paths outside
    the configured session/subtask sandbox.
    """
    if not path.is_file():
        raise ArtifactPathError(
            f"Cannot build ArtifactRef for {kind!r}: file does not exist: {path}"
        )
    effective_root = (root or get_artifact_root()).resolve()
    relative_uri = _relative_uri(effective_root, path, session_id, subtask_id)
    return ArtifactRef(
        kind=kind,
        path=path.resolve(),
        relative_uri=relative_uri,
        mime=mime or mime_for_path(path) or mime_for_kind(kind),
        size_bytes=path.stat().st_size,
        sha256=_sha256_of(path),
        producer_adapter=producer_adapter,
        producer_version=producer_version,
        created_at=created_at or datetime.now(UTC),
        metadata=dict(metadata or {}),
    )


def write_manifest(
    manifest: SubtaskArtifactManifest,
    *,
    root: Path | None = None,
) -> Path:
    """Serialize ``manifest`` to ``<root>/<session>/<subtask>/manifest.json``.

    Re-validates every artifact path lives under the subtask sandbox.
    """
    effective_root = (root or get_artifact_root()).resolve()
    sandbox = subtask_dir(
        effective_root, manifest.session_id, manifest.subtask_id
    ).resolve()
    for artifact in manifest.artifacts:
        try:
            artifact.path.resolve().relative_to(sandbox)
        except ValueError as exc:
            raise ArtifactPathError(
                f"ArtifactRef path {artifact.path} escapes subtask sandbox {sandbox}."
            ) from exc
    out = sandbox / MANIFEST_FILENAME
    out.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return out


def read_manifest(
    session_id: str,
    subtask_id: str,
    *,
    root: Path | None = None,
) -> SubtaskArtifactManifest:
    """Read ``manifest.json`` for the given session/subtask."""
    effective_root = (root or get_artifact_root()).resolve()
    sandbox = _subtask_dir_path(effective_root, session_id, subtask_id).resolve()
    path = sandbox / MANIFEST_FILENAME
    if not path.is_file():
        raise ManifestSchemaError(
            f"Manifest not found for session={session_id} subtask={subtask_id} at {path}."
        )
    raw = path.read_text(encoding="utf-8")
    try:
        manifest = SubtaskArtifactManifest.model_validate_json(raw)
    except ValidationError as exc:
        raise ManifestSchemaError(
            f"Manifest at {path} failed schema validation: {exc}"
        ) from exc
    if manifest.schema_version != MANIFEST_SCHEMA_VERSION:
        raise ManifestSchemaError(
            f"Manifest schema_version {manifest.schema_version!r} is not pinned "
            f"version {MANIFEST_SCHEMA_VERSION!r} (ADR-0005)."
        )
    return manifest


def resolve_artifact_path(
    session_id: str,
    subtask_id: str,
    filename: str,
    *,
    root: Path | None = None,
) -> Path:
    """Resolve a download path, enforcing the sandbox boundary.

    ``filename`` may include sub-segments separated by ``/`` (for nested
    artifacts), but each segment is validated against path-traversal.
    """
    effective_root = (root or get_artifact_root()).resolve()
    sandbox = _subtask_dir_path(effective_root, session_id, subtask_id).resolve()
    parts = [p for p in filename.replace("\\", "/").split("/") if p]
    for p in parts:
        if p in (".", "..") or p.startswith("/"):
            raise ArtifactPathError(f"Unsafe filename segment: {p!r}")
    if not parts:
        raise ArtifactPathError("filename must not be empty")
    candidate = (sandbox.joinpath(*parts)).resolve()
    try:
        candidate.relative_to(sandbox)
    except ValueError as exc:
        raise ArtifactPathError(
            f"Resolved artifact path {candidate} escapes sandbox {sandbox}."
        ) from exc
    if not candidate.is_file():
        raise ArtifactPathError(f"Artifact file not found: {candidate}")
    return candidate


def resolve_manifest_artifact_path(
    session_id: str,
    subtask_id: str,
    filename: str,
    *,
    root: Path | None = None,
    allowed_statuses: frozenset[ArtifactStatus] = frozenset({ArtifactStatus.OK}),
) -> Path:
    """Resolve a download path only when manifest.json allowlists it."""
    effective_root = (root or get_artifact_root()).resolve()
    manifest = read_manifest(session_id, subtask_id, root=effective_root)
    path = resolve_artifact_path(
        session_id, subtask_id, filename, root=effective_root
    )
    requested_uri = _relative_uri(effective_root, path, session_id, subtask_id)
    for artifact in manifest.artifacts:
        if artifact.relative_uri != requested_uri:
            continue
        if artifact.status not in allowed_statuses:
            raise ArtifactPathError(
                f"Artifact {requested_uri!r} is {artifact.status.value}; "
                "only ok artifacts are downloadable."
            )
        return path
    raise ArtifactPathError(
        f"Artifact {requested_uri!r} is not listed in manifest.json."
    )


def write_failure_manifest(
    *,
    session_id: str,
    subtask_id: str,
    stage: str,
    error_type: str,
    detail: str,
    error_metadata: dict[str, Any] | None = None,
    plan_snapshot_id: str | None = None,
    approval_gate_id: str | None = None,
    trace_id: str | None = None,
    root: Path | None = None,
) -> Path:
    """Emit a diagnostic manifest for a failed subtask (ADR-0005).

    Carries no artifacts and ``status=FAILED``. Lets UI / replay / audit
    tools find *why* a subtask failed alongside successful sibling
    manifests, instead of having to grep logs by trace_id.
    """
    effective_root = (root or get_artifact_root()).resolve()
    sandbox = subtask_dir(effective_root, session_id, subtask_id).resolve()
    manifest = SubtaskArtifactManifest(
        session_id=session_id,
        subtask_id=subtask_id,
        artifacts=(),
        created_at=datetime.now(UTC),
        status=ManifestStatus.FAILED,
        errors=(
            ManifestError(
                stage=stage,
                error_type=error_type,
                detail=detail,
                metadata=dict(error_metadata or {}),
            ),
        ),
        plan_snapshot_id=plan_snapshot_id,
        approval_gate_id=approval_gate_id,
        trace_id=trace_id,
    )
    out = sandbox / MANIFEST_FILENAME
    out.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return out


__all__ = [
    "DEFAULT_ARTIFACT_ROOT",
    "MANIFEST_FILENAME",
    "build_artifact_ref",
    "get_artifact_root",
    "read_manifest",
    "resolve_artifact_path",
    "resolve_manifest_artifact_path",
    "subtask_dir",
    "write_failure_manifest",
    "write_manifest",
]
