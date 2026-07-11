"""Artifact manifest contract (ADR-0005).

A subtask emits multiple artifacts (mechanical mesh, organic mesh, G-code,
3MF snapshot, validation report, preview image). This package owns the
single contract that wraps them all into ``SubtaskArtifactManifest`` so
the orchestrator API, the UI, and any future replay/archival tool read
from one schema.

See ``docs/decisions/ADR-0005-artifact-manifest.md``.
"""

from modules.artifacts.exceptions import (
    ArtifactError,
    ArtifactPathError,
    ManifestSchemaError,
)
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
from modules.artifacts.store import (
    DEFAULT_ARTIFACT_ROOT,
    MANIFEST_FILENAME,
    build_artifact_ref,
    get_artifact_root,
    read_manifest,
    resolve_artifact_path,
    resolve_manifest_artifact_path,
    subtask_dir,
    write_failure_manifest,
    write_manifest,
)

__all__ = [
    "DEFAULT_ARTIFACT_ROOT",
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA_VERSION",
    "ArtifactError",
    "ArtifactKind",
    "ArtifactPathError",
    "ArtifactRef",
    "ArtifactStatus",
    "ManifestError",
    "ManifestSchemaError",
    "ManifestStatus",
    "SubtaskArtifactManifest",
    "build_artifact_ref",
    "get_artifact_root",
    "mime_for_kind",
    "mime_for_path",
    "read_manifest",
    "resolve_artifact_path",
    "resolve_manifest_artifact_path",
    "subtask_dir",
    "write_failure_manifest",
    "write_manifest",
]
