from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from modules.newbie_request.asset_catalog import (
    DecorativeAssetLegalReviewStatus,
    DecorativeAssetPrintabilityStatus,
)
from modules.newbie_request.asset_draft_schemas import (
    AssetLifecycleStatus,
    DraftAssetEntry,
    DraftAssetReviewRecord,
    DraftReviewDecision,
)
from modules.newbie_request.printability import PrintabilityReport, PrintabilityStatus
from modules.newbie_request.schemas import VisualQualityStatus

_NEXT_GATES = (
    "visual_manual_pass",
    "legal_review_approved",
    "printability_smoke_passed",
    "runtime_catalog_registration",
)


def record_draft_review(
    draft: DraftAssetEntry,
    *,
    decision: DraftReviewDecision,
    reviewer: str,
    review_note: str,
    reviewed_at: datetime | None = None,
) -> DraftAssetReviewRecord:
    reviewed_at = reviewed_at or datetime.now(UTC)
    lifecycle = _lifecycle_from_decision(decision)
    return DraftAssetReviewRecord(
        draft_asset_id=draft.draft_asset_id,
        decision=decision,
        asset_lifecycle_status=lifecycle,
        reviewer=reviewer,
        reviewed_at=reviewed_at.isoformat(),
        review_note=review_note,
        required_next_gates=_NEXT_GATES,
        release_allowed=False,
        runtime_catalog_registered=False,
    )


class RegistrationGateStatus(StrEnum):
    BLOCKED = "registration_blocked"
    CANDIDATE = "registration_candidate"


class RegistrationBlockReason(StrEnum):
    NO_ACCEPT_REVIEW = "no_accept_review"
    LATEST_REVIEW_NOT_ACCEPT = "latest_review_not_accept"
    MISSING_MANUAL_VISUAL_PASS = "missing_manual_visual_pass"
    LEGAL_NOT_APPROVED = "legal_not_approved"
    PRINTABILITY_NOT_PASSED = "printability_not_passed"
    PRINTABILITY_EVIDENCE_MISSING = "printability_evidence_missing"
    PRINTABILITY_EVIDENCE_NOT_PASSED = "printability_evidence_not_passed"
    RIGHTS_NOT_VALID = "rights_not_valid"
    RELEASE_NOT_ALLOWED = "release_not_allowed"
    RUNTIME_CATALOG_ALREADY_REGISTERED = "runtime_catalog_already_registered"


class RegistrationGateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    draft: DraftAssetEntry
    review_records: tuple[DraftAssetReviewRecord, ...] = ()
    visual_quality_status: VisualQualityStatus
    legal_review_status: DecorativeAssetLegalReviewStatus
    printability_status: DecorativeAssetPrintabilityStatus
    printability_report: PrintabilityReport | None = None
    commercial_allowed: bool
    redistribution_allowed: bool
    modification_allowed: bool
    release_allowed: bool = False
    require_release_allowed: bool = False

    @model_validator(mode="after")
    def _records_belong_to_draft(self) -> RegistrationGateRequest:
        for record in self.review_records:
            if record.draft_asset_id != self.draft.draft_asset_id:
                raise ValueError("review record does not belong to draft")
        return self


class RegistrationGateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: RegistrationGateStatus
    draft_asset_id: str
    blocked_reasons: tuple[RegistrationBlockReason, ...] = ()
    release_allowed: bool = False
    runtime_catalog_registered: bool = False
    metadata: dict[str, object] = Field(default_factory=dict)

    @property
    def is_candidate(self) -> bool:
        return self.status is RegistrationGateStatus.CANDIDATE


def evaluate_registration_gate(
    request: RegistrationGateRequest,
) -> RegistrationGateResult:
    reasons: list[RegistrationBlockReason] = []

    current_review = _current_review(request.review_records)
    if current_review is None:
        reasons.append(RegistrationBlockReason.NO_ACCEPT_REVIEW)
    elif current_review.decision is not DraftReviewDecision.ACCEPT:
        reasons.append(RegistrationBlockReason.LATEST_REVIEW_NOT_ACCEPT)
    if request.visual_quality_status is not VisualQualityStatus.MANUAL_PASS:
        reasons.append(RegistrationBlockReason.MISSING_MANUAL_VISUAL_PASS)
    if (
        request.legal_review_status
        is not DecorativeAssetLegalReviewStatus.APPROVED
    ):
        reasons.append(RegistrationBlockReason.LEGAL_NOT_APPROVED)
    if (
        request.printability_status
        is not DecorativeAssetPrintabilityStatus.SMOKE_PASSED
    ):
        reasons.append(RegistrationBlockReason.PRINTABILITY_NOT_PASSED)
    elif request.printability_report is None:
        reasons.append(RegistrationBlockReason.PRINTABILITY_EVIDENCE_MISSING)
    elif (
        request.printability_report.status is not PrintabilityStatus.SMOKE_PASSED
        or not request.printability_report.can_slice
    ):
        reasons.append(RegistrationBlockReason.PRINTABILITY_EVIDENCE_NOT_PASSED)
    if not (
        request.commercial_allowed
        and request.redistribution_allowed
        and request.modification_allowed
    ):
        reasons.append(RegistrationBlockReason.RIGHTS_NOT_VALID)
    if request.require_release_allowed and not request.release_allowed:
        reasons.append(RegistrationBlockReason.RELEASE_NOT_ALLOWED)
    if request.draft.runtime_catalog_registered:
        reasons.append(RegistrationBlockReason.RUNTIME_CATALOG_ALREADY_REGISTERED)

    if reasons:
        return RegistrationGateResult(
            status=RegistrationGateStatus.BLOCKED,
            draft_asset_id=request.draft.draft_asset_id,
            blocked_reasons=tuple(dict.fromkeys(reasons)),
            release_allowed=False,
            runtime_catalog_registered=False,
            metadata=_gate_metadata(request),
        )

    return RegistrationGateResult(
        status=RegistrationGateStatus.CANDIDATE,
        draft_asset_id=request.draft.draft_asset_id,
        blocked_reasons=(),
        release_allowed=False,
        runtime_catalog_registered=False,
        metadata=_gate_metadata(request),
    )


def _current_review(
    records: tuple[DraftAssetReviewRecord, ...],
) -> DraftAssetReviewRecord | None:
    if not records:
        return None
    return max(records, key=lambda record: record.reviewed_at)


def _gate_metadata(request: RegistrationGateRequest) -> dict[str, object]:
    return {
        "review_count": len(request.review_records),
        "visual_quality_status": request.visual_quality_status.value,
        "legal_review_status": request.legal_review_status.value,
        "printability_status": request.printability_status.value,
        "printability_evidence_status": request.printability_report.status.value
        if request.printability_report is not None
        else None,
        "printability_can_slice": request.printability_report.can_slice
        if request.printability_report is not None
        else None,
        "commercial_allowed": request.commercial_allowed,
        "redistribution_allowed": request.redistribution_allowed,
        "modification_allowed": request.modification_allowed,
        "release_allowed_input": request.release_allowed,
        "require_release_allowed": request.require_release_allowed,
        "draft_generator": request.draft.draft_generator,
    }


def _lifecycle_from_decision(decision: DraftReviewDecision) -> AssetLifecycleStatus:
    if decision is DraftReviewDecision.REJECT:
        return AssetLifecycleStatus.REJECTED
    if decision is DraftReviewDecision.CANDIDATE:
        return AssetLifecycleStatus.REVIEW_CANDIDATE
    return AssetLifecycleStatus.ACCEPTED_FOR_REGISTRATION


__all__ = [
    "RegistrationBlockReason",
    "RegistrationGateRequest",
    "RegistrationGateResult",
    "RegistrationGateStatus",
    "evaluate_registration_gate",
    "record_draft_review",
]
