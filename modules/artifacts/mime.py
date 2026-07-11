"""MIME registry for Model Forge artifacts (ADR-0005)."""

from pathlib import Path

from modules.artifacts.schemas import ArtifactKind

_EXT_MIME: dict[str, str] = {
    ".stl": "model/stl",
    ".obj": "model/obj",
    ".3mf": "model/3mf",
    ".gcode": "text/x.gcode",
    ".json": "application/json",
    ".png": "image/png",
    ".glb": "model/gltf-binary",
}

_KIND_DEFAULT_MIME: dict[ArtifactKind, str] = {
    ArtifactKind.MECHANICAL_MESH: "model/stl",
    ArtifactKind.ORGANIC_MESH: "model/obj",
    ArtifactKind.MERGED_MESH: "model/stl",
    ArtifactKind.GCODE: "text/x.gcode",
    ArtifactKind.THREEMF: "model/3mf",
    ArtifactKind.VALIDATION_REPORT: "application/json",
    ArtifactKind.PREVIEW_MESH: "model/gltf-binary",
    ArtifactKind.PREVIEW_IMAGE: "image/png",
}


def mime_for_path(path: Path) -> str:
    """Return the MIME type for a file based on its extension.

    Unknown extensions fall back to ``application/octet-stream`` rather
    than raising — the manifest's ``mime`` field is authoritative; this
    helper is only the default.
    """
    return _EXT_MIME.get(path.suffix.lower(), "application/octet-stream")


def mime_for_kind(kind: ArtifactKind) -> str:
    """Return the canonical MIME for an :class:`ArtifactKind`."""
    return _KIND_DEFAULT_MIME[kind]


__all__ = ["mime_for_kind", "mime_for_path"]
