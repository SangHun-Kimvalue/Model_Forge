"""Bridge fallback gate output into the draft registration gate.

This module intentionally lives in ``modules.template`` so the dependency
direction remains ``template -> newbie_request``. The registration gate remains
the authority for candidacy; this bridge only adapts a fallback gate outcome
into the canonical registration request.
"""

from __future__ import annotations

from modules.newbie_request.asset_catalog import (
    DecorativeAssetLegalReviewStatus,
    DecorativeAssetPrintabilityStatus,
)
from modules.newbie_request.asset_draft_schemas import DraftAssetReviewRecord
from modules.newbie_request.asset_registration import (
    RegistrationGateRequest,
    RegistrationGateResult,
    evaluate_registration_gate,
)
from modules.newbie_request.printability import PrintabilityReport
from modules.newbie_request.schemas import VisualQualityStatus
from modules.template.fallback_verification_gate import FallbackGateOutcome


def promote_fallback_draft(
    outcome: FallbackGateOutcome,
    *,
    review_records: tuple[DraftAssetReviewRecord, ...],
    visual_quality_status: VisualQualityStatus,
    legal_review_status: DecorativeAssetLegalReviewStatus,
    printability_status: DecorativeAssetPrintabilityStatus,
    printability_report: PrintabilityReport | None = None,
    commercial_allowed: bool,
    redistribution_allowed: bool,
    modification_allowed: bool,
) -> RegistrationGateResult:
    """Evaluate fallback draft promotion by delegating to the existing gate.

    This function creates no new authority and performs no registration or
    release mutation. It only adapts the H1 fallback gate output into the
    canonical ``RegistrationGateRequest`` shape, then returns
    ``evaluate_registration_gate`` unchanged.
    """

    if outcome.draft is None:
        raise ValueError("fallback gate outcome has no draft to promote")

    request = RegistrationGateRequest(
        draft=outcome.draft,
        review_records=review_records,
        visual_quality_status=visual_quality_status,
        legal_review_status=legal_review_status,
        printability_status=printability_status,
        printability_report=printability_report,
        commercial_allowed=commercial_allowed,
        redistribution_allowed=redistribution_allowed,
        modification_allowed=modification_allowed,
    )
    return evaluate_registration_gate(request)


__all__ = ["promote_fallback_draft"]
