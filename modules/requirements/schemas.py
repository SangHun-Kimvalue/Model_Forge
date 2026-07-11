from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DimensionMode = Literal["exact", "minimum", "maximum"]


class DimensionRequirement(BaseModel):
    """Structured dimensional intent extracted from a user prompt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    value_mm: float = Field(gt=0)
    mode: DimensionMode = "exact"
    tolerance_mm: float | None = Field(default=None, ge=0)


class QualityCheck(BaseModel):
    """A semantic or geometric check requested by the requirement extractor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class RequirementSpec(BaseModel):
    """Traceable user intent passed from planner/CAD generation to QA gates."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    object_type: str | None = None
    intent_tags: tuple[str, ...] = ()
    required_features: tuple[str, ...] = ()
    forbidden_outcomes: tuple[str, ...] = ()
    dimensions: tuple[DimensionRequirement, ...] = ()
    print_constraints: tuple[str, ...] = ()
    quality_checks: tuple[QualityCheck, ...] = ()
    needs_clarification: bool = False
    clarifying_questions: tuple[str, ...] = ()
    trace_id: str | None = None

    @field_validator(
        "object_type",
        mode="before",
    )
    @classmethod
    def _normalize_optional_token(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = _normalize_token(value)
            return normalized or None
        return value

    @field_validator(
        "intent_tags",
        "required_features",
        "forbidden_outcomes",
        "print_constraints",
        mode="before",
    )
    @classmethod
    def _normalize_token_tuple(cls, value: object) -> object:
        if value is None:
            return ()
        if isinstance(value, str):
            value = (value,)
        if isinstance(value, (list, tuple, set)):
            seen: set[str] = set()
            normalized: list[str] = []
            for item in value:
                if not isinstance(item, str):
                    normalized.append(str(item))
                    continue
                token = _normalize_token(item)
                if token and token not in seen:
                    seen.add(token)
                    normalized.append(token)
            return tuple(normalized)
        return value


class RequirementExtractionRequest(BaseModel):
    """Input for a requirement extraction adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None


def _normalize_token(value: str) -> str:
    stripped = value.strip().lower()
    stripped = re.sub(r"[\s\-]+", "_", stripped)
    stripped = re.sub(r"[^0-9a-zA-Z_가-힣]+", "", stripped)
    return stripped.strip("_")


__all__ = [
    "DimensionMode",
    "DimensionRequirement",
    "QualityCheck",
    "RequirementExtractionRequest",
    "RequirementSpec",
]
