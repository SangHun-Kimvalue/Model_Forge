"""Trimesh-backed mesh validator (Phase 9A).

Performs real geometry checks using the ``trimesh`` library instead of the
pure-Python ASCII parser in the mock adapter.  Provides more accurate manifold
detection (actual topology check, not just edge-multiplicity counting) and
real bounding-box wall-thickness heuristics.

Install: ``pip install model_forge[trimesh]`` (adds ``trimesh>=4.0,<5``).

If trimesh is not installed:
    - ``health_check()`` returns False.
    - ``validate()`` raises ``ValidatorConfigError`` with an install hint.

Supported formats: stl, obj.
Unsupported formats raise ``UnsupportedFormatError`` (R10, no silent skip).

Wall-thickness check:
    Uses the mesh bounding-box minimum extent as a proxy, same as the mock
    adapter.  A proper ray-based wall-thickness sampler is planned for a
    later phase; the current heuristic is still much more accurate than the
    mock's pure-ASCII bbox because it operates on the fully loaded mesh.

Normal-consistency check:
    Uses ``trimesh.Trimesh.is_winding_consistent``.  This detects faces with
    inconsistent winding order but not necessarily all normal-direction issues;
    results in a WARNING rather than ERROR to avoid blocking valid prints.
"""

from __future__ import annotations

import time
from typing import Any

from modules.validator.base import BaseMeshValidator
from modules.validator.exceptions import (
    UnsupportedFormatError,
    ValidatorConfigError,
    ValidatorIOError,
)
from modules.validator.schemas import (
    IssueSeverity,
    MeshFormat,
    ValidationCheck,
    ValidationIssue,
    ValidationReport,
    ValidationRequest,
)

__all__ = ["TrimeshValidatorAdapter"]

_SUPPORTED_FORMATS: frozenset[MeshFormat] = frozenset({"stl", "obj"})


class TrimeshValidatorAdapter(BaseMeshValidator):
    """Real mesh validator backed by the ``trimesh`` library."""

    default_adapter_name = "trimesh-validator-v1"

    def __init__(self, adapter_label: str | None = None) -> None:
        self._adapter_label = adapter_label or self.default_adapter_name

    @property
    def adapter_name(self) -> str:
        return self._adapter_label

    def health_check(self) -> bool:
        """Return True only when trimesh can be imported."""
        try:
            import trimesh  # noqa: F401

            return True
        except ImportError:
            return False

    async def validate(self, request: ValidationRequest) -> ValidationReport:
        trimesh_mod = _require_trimesh()

        if request.mesh_format not in _SUPPORTED_FORMATS:
            raise UnsupportedFormatError(
                f"TrimeshValidatorAdapter does not handle mesh_format="
                f"'{request.mesh_format}'. Supported: {sorted(_SUPPORTED_FORMATS)}. "
                "STEP requires a CAD-aware adapter."
            )
        if not request.mesh_path.exists():
            raise ValidatorIOError(
                f"mesh_path '{request.mesh_path}' does not exist."
            )
        if not request.mesh_path.is_file():
            raise ValidatorIOError(
                f"mesh_path '{request.mesh_path}' is not a regular file."
            )

        start = time.perf_counter()
        try:
            loaded = trimesh_mod.load(str(request.mesh_path), force="mesh")
        except Exception as exc:
            raise ValidatorIOError(
                f"trimesh could not load '{request.mesh_path}': {exc}"
            ) from exc

        if not isinstance(loaded, trimesh_mod.Trimesh):
            # trimesh.load may return a Scene when the file has multiple objects
            raise ValidatorIOError(
                f"'{request.mesh_path}' loaded as {type(loaded).__name__}, "
                "not a single Trimesh.  Use a file with one mesh object."
            )

        mesh: Any = loaded
        issues: list[ValidationIssue] = []

        if ValidationCheck.MANIFOLD in request.checks:
            if not mesh.is_watertight:
                issues.append(
                    ValidationIssue(
                        check=ValidationCheck.MANIFOLD,
                        severity=IssueSeverity.ERROR,
                        message=(
                            f"mesh is not watertight (trimesh.is_watertight=False). "
                            f"face_count={len(mesh.faces)}, "
                            f"vertex_count={len(mesh.vertices)}."
                        ),
                        location={
                            "face_count": len(mesh.faces),
                            "vertex_count": len(mesh.vertices),
                        },
                    )
                )

        if ValidationCheck.WALL_THICKNESS in request.checks:
            extents = mesh.bounding_box.extents
            min_dim = float(extents.min())
            if min_dim < request.min_wall_thickness_mm:
                issues.append(
                    ValidationIssue(
                        check=ValidationCheck.WALL_THICKNESS,
                        severity=IssueSeverity.WARNING,
                        message=(
                            f"bounding-box minimum extent {min_dim:.3f} mm is "
                            f"below the required {request.min_wall_thickness_mm} mm "
                            "(bounding-box heuristic; ray-based sampler pending)."
                        ),
                        location={"min_bbox_dimension_mm": min_dim},
                    )
                )

        if ValidationCheck.NORMAL_CONSISTENCY in request.checks:
            if not mesh.is_winding_consistent:
                issues.append(
                    ValidationIssue(
                        check=ValidationCheck.NORMAL_CONSISTENCY,
                        severity=IssueSeverity.WARNING,
                        message=(
                            "mesh has inconsistent face winding "
                            "(trimesh.is_winding_consistent=False)."
                        ),
                    )
                )

        has_error = any(i.severity is IssueSeverity.ERROR for i in issues)
        volume: float | None = float(mesh.volume) if mesh.is_watertight else None

        return ValidationReport(
            passed=not has_error,
            mesh_path=request.mesh_path,
            mesh_format=request.mesh_format,
            triangle_count=len(mesh.faces),
            is_watertight=mesh.is_watertight,
            issues=tuple(issues),
            adapter_used=self._adapter_label,
            duration_s=time.perf_counter() - start,
            metadata={
                "vertex_count": len(mesh.vertices),
                "volume": volume,
                "checks_requested": sorted(c.value for c in request.checks),
            },
        )


def _require_trimesh() -> Any:
    try:
        import trimesh

        return trimesh
    except ImportError as exc:
        raise ValidatorConfigError(
            "trimesh is not installed. Install it with: "
            "pip install 'model_forge[trimesh]'"
        ) from exc
