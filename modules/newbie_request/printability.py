from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from modules.newbie_request.schemas import L2GeometryReport


class PrintabilityStatus(StrEnum):
    BLOCKED = "printability_blocked"
    SMOKE_PASSED = "printability_smoke_passed"
    REPAIR_CANDIDATE = "repair_candidate"


class PrintabilityIssueSeverity(StrEnum):
    BLOCKED = "blocked"
    REPAIR_CANDIDATE = "repair_candidate"


class PrintabilityIssueCode(StrEnum):
    STL_MISSING = "stl_missing"
    STL_PATH_INVALID = "stl_path_invalid"
    L2_GEOMETRY_FAILED = "l2_geometry_failed"
    EMPTY_OR_UNPARSED_GEOMETRY = "empty_or_unparsed_geometry"
    DEGENERATE_EXTENTS = "degenerate_extents"
    VOLUME_TOO_LOW = "volume_too_low"
    TRIANGLE_COUNT_TOO_LOW = "triangle_count_too_low"
    VERTEX_COUNT_TOO_LOW = "vertex_count_too_low"
    WATERTIGHT_NOT_EVALUATED = "watertight_not_evaluated"
    WATERTIGHT_FAILED = "watertight_failed"


class PrintabilityGateError(ValueError):
    """Raised when a generated mesh must not enter slicer."""


class PrintabilityIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: PrintabilityIssueCode
    severity: PrintabilityIssueSeverity
    message: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PrintabilityGuardPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_extent_mm: float = Field(default=0.1, ge=0)
    min_volume_mm3: float = Field(default=1.0, ge=0)
    min_triangle_count: int = Field(default=4, ge=1)
    min_vertex_count: int = Field(default=4, ge=1)
    require_watertight: bool = False


class PrintabilityReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: PrintabilityStatus
    can_slice: bool
    repair_required: bool
    issues: tuple[PrintabilityIssue, ...] = ()
    extents_mm: tuple[float, float, float] | None = None
    volume_mm3: float | None = Field(default=None, ge=0)
    watertight: bool | None = None
    triangle_count: int | None = Field(default=None, ge=0)
    vertex_count: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _slice_requires_smoke_pass(self) -> PrintabilityReport:
        if self.can_slice and self.status is not PrintabilityStatus.SMOKE_PASSED:
            raise ValueError("can_slice requires printability_smoke_passed")
        if self.status is PrintabilityStatus.SMOKE_PASSED and not self.can_slice:
            raise ValueError("printability_smoke_passed requires can_slice=true")
        if self.status is PrintabilityStatus.SMOKE_PASSED and self.repair_required:
            raise ValueError(
                "printability_smoke_passed cannot require repair"
            )
        if self.status is PrintabilityStatus.SMOKE_PASSED and self.issues:
            raise ValueError("printability_smoke_passed cannot carry issues")
        return self


def evaluate_printability(
    l2_report: L2GeometryReport,
    *,
    policy: PrintabilityGuardPolicy | None = None,
) -> PrintabilityReport:
    """Convert cheap STL evidence into a slicer-entry printability decision."""

    active_policy = policy or PrintabilityGuardPolicy()
    issues: list[PrintabilityIssue] = []

    if not l2_report.stl_exists:
        issues.append(
            _issue(
                PrintabilityIssueCode.STL_MISSING,
                PrintabilityIssueSeverity.BLOCKED,
                "STL artifact is missing.",
            )
        )
    if l2_report.stl_exists and l2_report.stl_size_bytes <= 0:
        issues.append(
            _issue(
                PrintabilityIssueCode.STL_PATH_INVALID,
                PrintabilityIssueSeverity.BLOCKED,
                "STL artifact is empty or not a regular generated file.",
            )
        )
    if not l2_report.passed:
        issues.append(
            _issue(
                PrintabilityIssueCode.L2_GEOMETRY_FAILED,
                PrintabilityIssueSeverity.BLOCKED,
                "L2 geometry smoke did not pass.",
                {"l2_metadata": dict(l2_report.metadata)},
            )
        )

    extents = l2_report.extents_mm
    if extents is None:
        issues.append(
            _issue(
                PrintabilityIssueCode.EMPTY_OR_UNPARSED_GEOMETRY,
                PrintabilityIssueSeverity.BLOCKED,
                "No usable STL vertices or extents were parsed.",
            )
        )
    elif any(value <= active_policy.min_extent_mm for value in extents):
        issues.append(
            _issue(
                PrintabilityIssueCode.DEGENERATE_EXTENTS,
                PrintabilityIssueSeverity.BLOCKED,
                "One or more STL extents are too small for printability smoke.",
                {"extents_mm": extents, "min_extent_mm": active_policy.min_extent_mm},
            )
        )

    if l2_report.volume_mm3 is None or l2_report.volume_mm3 <= 0:
        issues.append(
            _issue(
                PrintabilityIssueCode.VOLUME_TOO_LOW,
                PrintabilityIssueSeverity.BLOCKED,
                "STL volume is missing or zero.",
                {"volume_mm3": l2_report.volume_mm3},
            )
        )
    elif l2_report.volume_mm3 < active_policy.min_volume_mm3:
        issues.append(
            _issue(
                PrintabilityIssueCode.VOLUME_TOO_LOW,
                PrintabilityIssueSeverity.REPAIR_CANDIDATE,
                "STL volume is below the printability smoke threshold.",
                {
                    "volume_mm3": l2_report.volume_mm3,
                    "min_volume_mm3": active_policy.min_volume_mm3,
                },
            )
        )

    if (
        l2_report.triangle_count is None
        or l2_report.triangle_count < active_policy.min_triangle_count
    ):
        issues.append(
            _issue(
                PrintabilityIssueCode.TRIANGLE_COUNT_TOO_LOW,
                PrintabilityIssueSeverity.REPAIR_CANDIDATE,
                "Triangle count is below the printability smoke threshold.",
                {
                    "triangle_count": l2_report.triangle_count,
                    "min_triangle_count": active_policy.min_triangle_count,
                },
            )
        )
    if (
        l2_report.vertex_count is None
        or l2_report.vertex_count < active_policy.min_vertex_count
    ):
        issues.append(
            _issue(
                PrintabilityIssueCode.VERTEX_COUNT_TOO_LOW,
                PrintabilityIssueSeverity.REPAIR_CANDIDATE,
                "Vertex count is below the printability smoke threshold.",
                {
                    "vertex_count": l2_report.vertex_count,
                    "min_vertex_count": active_policy.min_vertex_count,
                },
            )
        )

    if active_policy.require_watertight:
        if l2_report.watertight is None:
            issues.append(
                _issue(
                    PrintabilityIssueCode.WATERTIGHT_NOT_EVALUATED,
                    PrintabilityIssueSeverity.REPAIR_CANDIDATE,
                    "Watertightness is required but has not been evaluated.",
                )
            )
        elif l2_report.watertight is False:
            issues.append(
                _issue(
                    PrintabilityIssueCode.WATERTIGHT_FAILED,
                    PrintabilityIssueSeverity.REPAIR_CANDIDATE,
                    "Watertightness check failed.",
                )
            )

    if any(issue.severity is PrintabilityIssueSeverity.BLOCKED for issue in issues):
        status = PrintabilityStatus.BLOCKED
    elif issues:
        status = PrintabilityStatus.REPAIR_CANDIDATE
    else:
        status = PrintabilityStatus.SMOKE_PASSED

    return PrintabilityReport(
        status=status,
        can_slice=status is PrintabilityStatus.SMOKE_PASSED,
        repair_required=status is PrintabilityStatus.REPAIR_CANDIDATE,
        issues=tuple(issues),
        extents_mm=extents,
        volume_mm3=l2_report.volume_mm3,
        watertight=l2_report.watertight,
        triangle_count=l2_report.triangle_count,
        vertex_count=l2_report.vertex_count,
        metadata={
            "guard": "newbie_request_printability_guard",
            "guard_version": "0.1.0",
            "policy": active_policy.model_dump(mode="json"),
            "l2_passed": l2_report.passed,
            "slicer_entry_allowed": status is PrintabilityStatus.SMOKE_PASSED,
        },
    )


def printability_manifest_metadata(
    report: PrintabilityReport | None,
) -> dict[str, object] | None:
    if report is None:
        return None
    return report.model_dump(mode="json")


def printability_allows_slicing(report: PrintabilityReport | None) -> bool:
    return report is not None and report.can_slice


def require_printability_for_slicing(
    report: PrintabilityReport | None,
) -> PrintabilityReport:
    if report is None:
        raise PrintabilityGateError(
            "printability evidence is required before slicer entry"
        )
    if not report.can_slice:
        raise PrintabilityGateError(
            f"printability guard blocked slicer entry: {report.status.value}"
        )
    return report


def printability_gcode_metadata(
    report: PrintabilityReport | None,
    *,
    requested_stage: str,
    requested_reason: str,
) -> dict[str, str]:
    if report is None and _is_success_like_gcode_stage(requested_stage):
        return {
            "stage": "blocked",
            "reason": "printability_guard_evidence_missing",
        }
    if report is not None and not report.can_slice:
        return {
            "stage": "blocked",
            "reason": f"printability_guard_{report.status.value}",
        }
    return {
        "stage": requested_stage,
        "reason": requested_reason,
    }


def _issue(
    code: PrintabilityIssueCode,
    severity: PrintabilityIssueSeverity,
    message: str,
    metadata: dict[str, Any] | None = None,
) -> PrintabilityIssue:
    return PrintabilityIssue(
        code=code,
        severity=severity,
        message=message,
        metadata=metadata or {},
    )


def _is_success_like_gcode_stage(stage: str) -> bool:
    return stage.strip().lower() in {
        "completed",
        "complete",
        "done",
        "generated",
        "sliced",
        "success",
    }


__all__ = [
    "PrintabilityGateError",
    "PrintabilityGuardPolicy",
    "PrintabilityIssue",
    "PrintabilityIssueCode",
    "PrintabilityIssueSeverity",
    "PrintabilityReport",
    "PrintabilityStatus",
    "evaluate_printability",
    "printability_allows_slicing",
    "printability_gcode_metadata",
    "printability_manifest_metadata",
    "require_printability_for_slicing",
]
