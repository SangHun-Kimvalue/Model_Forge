"""Artifact manifest error hierarchy (ADR-0005)."""


class ArtifactError(Exception):
    """Base class for every artifact-manifest failure."""


class ArtifactPathError(ArtifactError):
    """Raised when an artifact path escapes the session/subtask root.

    Path traversal is treated as a hard failure (ADR-0005 invariant #1).
    Callers MUST NOT swallow this — the manifest writer would otherwise
    let a producer point at, say, ``/etc/passwd`` and have the orchestrator
    serve it through ``GET /artifacts/...``.
    """


class ManifestSchemaError(ArtifactError):
    """Raised when an on-disk manifest cannot be parsed or has the wrong version."""
