from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from modules.newbie_request.schemas import VisualQualityStatus

_PASS_STATUSES = frozenset(
    {
        VisualQualityStatus.MANUAL_PASS.value,
        VisualQualityStatus.AUTOMATED_PASS.value,
    }
)


class VisualReviewCriterion(StrEnum):
    SUBJECT_READABILITY = "subject_readability"
    SILHOUETTE_QUALITY = "silhouette_quality"
    FACIAL_DETAIL_QUALITY = "facial_detail_quality"
    PRINTABILITY = "printability"
    PRESENTATION_READINESS = "presentation_readiness"
    BRAND_SAFETY_IP_RISK = "brand_safety_ip_risk"


class VisualReviewRubricItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion: VisualReviewCriterion
    score: int = Field(ge=0, le=5)
    passed: bool
    note: str = Field(min_length=1)


DECORATIVE_KEYRING_RUBRIC: tuple[VisualReviewCriterion, ...] = (
    VisualReviewCriterion.SUBJECT_READABILITY,
    VisualReviewCriterion.SILHOUETTE_QUALITY,
    VisualReviewCriterion.FACIAL_DETAIL_QUALITY,
    VisualReviewCriterion.PRINTABILITY,
    VisualReviewCriterion.PRESENTATION_READINESS,
    VisualReviewCriterion.BRAND_SAFETY_IP_RISK,
)


def visual_quality_metadata(
    *,
    required: bool,
    required_features: Sequence[str] = (),
    status: VisualQualityStatus | None = None,
    reason: str | None = None,
) -> dict[str, object]:
    effective_status = status or (
        VisualQualityStatus.REVIEW_REQUIRED
        if required
        else VisualQualityStatus.NOT_EVALUATED
    )
    effective_reason = reason or (
        "decorative_or_keyring_requires_manual_or_automated_judge"
        if required
        else "not_required_for_basic_mechanical_template"
    )
    return {
        "visual_quality_required": required,
        "visual_quality_status": effective_status.value,
        "visual_quality_reason": effective_reason,
        "review_required_features": tuple(required_features) if required else (),
        "reviewer_type": None,
        "reviewed_by": None,
        "reviewed_at": None,
    }


def apply_manual_visual_review(
    metadata: Mapping[str, Any],
    *,
    status: VisualQualityStatus,
    reviewer_type: str = "manual",
    reviewed_by: str | None = None,
    reviewed_at: datetime | None = None,
    reason: str | None = None,
    review_note: str | None = None,
    rubric_items: Sequence[VisualReviewRubricItem] = (),
) -> dict[str, object]:
    if status not in {
        VisualQualityStatus.MANUAL_PASS_CANDIDATE,
        VisualQualityStatus.MANUAL_PASS,
        VisualQualityStatus.MANUAL_FAIL,
    }:
        raise ValueError(
            "Manual visual review status must be manual_pass_candidate, "
            "manual_pass, or manual_fail."
        )

    timestamp = reviewed_at or datetime.now(UTC)
    updated = dict(metadata)
    updated.update(
        {
            "visual_quality_status": status.value,
            "visual_quality_reason": reason
            or (
                "manual_review_passed"
                if status is VisualQualityStatus.MANUAL_PASS
                else (
                    "manual_review_pass_candidate"
                    if status is VisualQualityStatus.MANUAL_PASS_CANDIDATE
                    else "manual_review_failed"
                )
            ),
            "reviewer_type": reviewer_type,
            "reviewed_by": reviewed_by,
            "reviewed_at": timestamp.isoformat(),
            "review_note": review_note,
            "visual_review_rubric": tuple(
                item.model_dump(mode="json") for item in rubric_items
            ),
        }
    )
    if "release_allowed" in updated and status is not VisualQualityStatus.MANUAL_PASS:
        updated["release_allowed"] = False
    return updated


def is_visual_quality_product_ready(metadata: Mapping[str, Any]) -> bool:
    if metadata.get("visual_quality_required") is not True:
        return True
    status = metadata.get("visual_quality_status")
    return isinstance(status, str) and status in _PASS_STATUSES


__all__ = [
    "DECORATIVE_KEYRING_RUBRIC",
    "VisualReviewCriterion",
    "VisualReviewRubricItem",
    "apply_manual_visual_review",
    "is_visual_quality_product_ready",
    "visual_quality_metadata",
]
