from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MeshFormat = Literal["stl", "obj", "step"]


class IssueSeverity(StrEnum):
    """Severity of a validation issue.

    ``error`` issues block downstream slicing; the orchestrator routes
    them back into the self-healing loop. ``warning`` and ``info`` are
    surfaced for human review but do not block.
    """

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ValidationCheck(StrEnum):
    """Validation checks performed by a ``BaseMeshValidator``.

    The set is intentionally closed at Phase 5 — adding a check is a
    schema change that must update every adapter and every consumer.
    """

    MANIFOLD = "manifold"
    WALL_THICKNESS = "wall_thickness"
    NORMAL_CONSISTENCY = "normal_consistency"


_ALL_CHECKS: frozenset[ValidationCheck] = frozenset(ValidationCheck)


class ValidationRequest(BaseModel):
    """Provider-neutral mesh validation request.

    Preconditions:
        ``mesh_path`` exists and contains a mesh in ``mesh_format``. The
        validator does not generate meshes — that's modules C / C'.
        ``min_wall_thickness_mm`` defaults to 2× nozzle diameter, the
        practical floor for FDM at 0.4 mm nozzles (DESIGN.md §4.6).
    Raises:
        pydantic.ValidationError if fields are missing or out of range.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mesh_path: Path
    mesh_format: MeshFormat
    session_id: str = Field(min_length=1)
    nozzle_diameter_mm: float = Field(default=0.4, gt=0.0, le=2.0)
    min_wall_thickness_mm: float = Field(default=0.8, gt=0.0, le=10.0)
    checks: frozenset[ValidationCheck] = Field(default=_ALL_CHECKS)

    @model_validator(mode="after")
    def _checks_must_be_non_empty(self) -> "ValidationRequest":
        if not self.checks:
            raise ValueError("ValidationRequest.checks must list at least one check")
        return self


class ValidationIssue(BaseModel):
    """Single validation finding produced by a ``BaseMeshValidator``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    check: ValidationCheck
    severity: IssueSeverity
    message: str = Field(min_length=1)
    location: dict[str, Any] = Field(default_factory=dict)


class ValidationReport(BaseModel):
    """Outcome of validating one mesh.

    ``passed`` must be ``False`` whenever any contained issue has
    ``severity == ERROR``. This invariant is enforced by the model so an
    adapter cannot return a contradictory ``passed=True`` alongside an
    error-level issue (cf. ReviewerResponse contract in agents).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    mesh_path: Path
    mesh_format: MeshFormat
    triangle_count: int = Field(ge=0)
    is_watertight: bool | None
    issues: tuple[ValidationIssue, ...] = ()
    adapter_used: str = Field(min_length=1)
    duration_s: float = Field(ge=0.0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _passed_consistent_with_errors(self) -> "ValidationReport":
        has_error = any(issue.severity is IssueSeverity.ERROR for issue in self.issues)
        if has_error and self.passed:
            raise ValueError(
                "ValidationReport.passed=True but at least one issue has "
                "severity=error; the report is internally inconsistent."
            )
        return self


class RepairRequest(BaseModel):
    """Request a mesh repair pass (hole filling, normal fix, etc.)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mesh_path: Path
    mesh_format: MeshFormat
    output_dir: Path
    session_id: str = Field(min_length=1)


class RepairResult(BaseModel):
    """Outcome of a repair pass."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repaired_path: Path
    mesh_format: MeshFormat
    operations_applied: tuple[str, ...]
    adapter_used: str = Field(min_length=1)
    duration_s: float = Field(ge=0.0)
    metadata: dict[str, Any] = Field(default_factory=dict)
