"""Artifact manifest pydantic schemas (ADR-0005)."""

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MANIFEST_SCHEMA_VERSION: Literal["1"] = "1"


class ArtifactKind(StrEnum):
    """Canonical artifact taxonomy (ADR-0005).

    Adding a new value is non-breaking. Repurposing an existing value
    requires a new manifest schema version.
    """

    MECHANICAL_MESH = "mechanical_mesh"
    ORGANIC_MESH = "organic_mesh"
    MERGED_MESH = "merged_mesh"
    GCODE = "gcode"
    THREEMF = "threemf"
    VALIDATION_REPORT = "validation_report"
    PREVIEW_MESH = "preview_mesh"
    PREVIEW_IMAGE = "preview_image"


class ArtifactStatus(StrEnum):
    """Per-artifact health (ADR-0005).

    UI must surface ``PARTIAL`` / ``QUARANTINED`` as diagnostic-only —
    never as a slice/print/download success.
    """

    OK = "ok"
    PARTIAL = "partial"
    QUARANTINED = "quarantined"


class ManifestStatus(StrEnum):
    """Subtask-level execution status (ADR-0005).

    ``FAILED`` manifests carry diagnostic ``errors`` instead of usable
    artifacts. ``PARTIAL`` means some artifacts succeeded; UI must not
    treat the bundle as a printable output.
    """

    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"
    QUARANTINED = "quarantined"


class ManifestError(BaseModel):
    """Diagnostic error attached to a failed/partial manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: str = Field(min_length=1)
    error_type: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactRef(BaseModel):
    """A single artifact a subtask produced.

    ``path`` is the absolute path on the local filesystem. ``relative_uri``
    is the URI exposed to API/UI consumers (``artifacts/<session>/<subtask>/
    <filename>``). UI MUST consume ``relative_uri`` rather than computing
    URLs from ``path`` (ADR-0005 invariant #5).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ArtifactKind
    path: Path
    relative_uri: str = Field(min_length=1)
    mime: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    producer_adapter: str = Field(min_length=1)
    producer_version: str | None = None
    status: ArtifactStatus = ArtifactStatus.OK
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class SubtaskArtifactManifest(BaseModel):
    """All artifacts a single subtask produced (ADR-0005).

    Phase 9D close prep added ``status`` / ``errors`` / ``plan_snapshot_id``
    / ``approval_gate_id`` / ``trace_id`` so a manifest captures *what*
    was produced *under which approval snapshot* and *why* it failed when
    it did. All fields default to backward-compatible values; manifests
    written by Phase 9B-vanilla code still load.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1)
    subtask_id: str = Field(min_length=1)
    schema_version: Literal["1"] = MANIFEST_SCHEMA_VERSION
    artifacts: tuple[ArtifactRef, ...]
    created_at: datetime
    status: ManifestStatus = ManifestStatus.SUCCESS
    errors: tuple[ManifestError, ...] = ()
    plan_snapshot_id: str | None = None
    approval_gate_id: str | None = None
    trace_id: str | None = None

    def find(self, kind: ArtifactKind) -> ArtifactRef | None:
        """Return the first artifact matching ``kind`` or ``None``."""
        for a in self.artifacts:
            if a.kind is kind:
                return a
        return None

    def legacy_pipeline_payload(self) -> dict[str, str | None]:
        """Return the legacy flat payload kept for one release (ADR-0005).

        Populates ``stl_path`` from MECHANICAL_MESH (falling back to MERGED),
        ``gcode_path`` from GCODE, ``threemf_path`` from THREEMF. Adapter
        names are pulled from the matching ArtifactRef.producer_adapter.

        Drop site: Phase 9D close.
        """
        mech = self.find(ArtifactKind.MECHANICAL_MESH) or self.find(
            ArtifactKind.MERGED_MESH
        )
        gcode = self.find(ArtifactKind.GCODE)
        threemf = self.find(ArtifactKind.THREEMF)
        return {
            "stl_path": str(mech.path) if mech else None,
            "gcode_path": str(gcode.path) if gcode else None,
            "threemf_path": str(threemf.path) if threemf else None,
            "adapter_cad": mech.producer_adapter if mech else None,
            "adapter_slicer": gcode.producer_adapter if gcode else None,
        }


__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "ArtifactKind",
    "ArtifactRef",
    "ArtifactStatus",
    "ManifestError",
    "ManifestStatus",
    "SubtaskArtifactManifest",
]
