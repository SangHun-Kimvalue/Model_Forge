from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from modules.requirements.schemas import RequirementSpec

SemanticFailureType = Literal[
    "missing_required_features",
    "forbidden_outcome",
    "constraint_violation",
]


class SemanticValidationRequest(BaseModel):
    """Input for semantic validation of generated CAD output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = ""
    requirements: RequirementSpec | None = None
    stl_path: str | None = None
    stl_extents_mm: tuple[float, float, float] | None = None
    stl_triangle_count: int | None = Field(default=None, ge=0)
    stl_vertex_count: int | None = Field(default=None, ge=0)
    trace_id: str | None = None


class SemanticValidationReport(BaseModel):
    """Result of checking generated CAD against user-visible intent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    missing_features: tuple[str, ...] = ()
    violated_constraints: tuple[str, ...] = ()
    failure_type: SemanticFailureType | None = None
    retry_hint: str | None = None
    trace_id: str | None = None

    @model_validator(mode="after")
    def validate_pass_failure_signals(self) -> SemanticValidationReport:
        if not self.passed:
            return self

        if self.missing_features:
            raise ValueError("passed semantic report cannot include missing_features.")
        if self.violated_constraints:
            raise ValueError("passed semantic report cannot include violated_constraints.")
        if self.failure_type is not None:
            raise ValueError("passed semantic report cannot include failure_type.")
        if self.retry_hint is not None:
            raise ValueError("passed semantic report cannot include retry_hint.")
        return self


__all__ = [
    "SemanticFailureType",
    "SemanticValidationReport",
    "SemanticValidationRequest",
]
