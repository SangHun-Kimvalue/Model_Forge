from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from modules.newbie_request.asset_catalog import (
    DecorativeAssetLegalReviewStatus,
    DecorativeAssetPrintabilityStatus,
    DecorativeAssetSourceType,
)
from modules.newbie_request.schemas import NewbieRoute, SizeMM, VisualQualityStatus


class AssetLifecycleStatus(StrEnum):
    DRAFT = "draft"
    REVIEW_CANDIDATE = "review_candidate"
    REJECTED = "rejected"
    ACCEPTED_FOR_REGISTRATION = "accepted_for_registration"


class DraftReviewDecision(StrEnum):
    REJECT = "reject"
    CANDIDATE = "candidate"
    ACCEPT = "accept"


class DraftAssetEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    draft_asset_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_]*$")
    asset_lifecycle_status: AssetLifecycleStatus = AssetLifecycleStatus.DRAFT
    subject: str = Field(min_length=1)
    category: str = Field(min_length=1)
    style: str = Field(min_length=1)
    source_type: DecorativeAssetSourceType
    license: str = Field(min_length=1)
    source_url_or_owner: str = Field(min_length=1)
    provenance_note: str = Field(min_length=1)
    originating_prompt: str = Field(min_length=1)
    draft_generator: str = Field(min_length=1)
    draft_renderer_version: str = Field(min_length=1)
    review_queue_reason: str = Field(min_length=1)
    default_size_mm: SizeMM
    min_size_mm: SizeMM
    recommended_thickness_mm: float = Field(gt=0)
    required_features: tuple[str, ...] = Field(min_length=1)
    visual_quality_status: VisualQualityStatus = VisualQualityStatus.REVIEW_REQUIRED
    legal_review_status: DecorativeAssetLegalReviewStatus = (
        DecorativeAssetLegalReviewStatus.NOT_REVIEWED
    )
    printability_status: DecorativeAssetPrintabilityStatus = (
        DecorativeAssetPrintabilityStatus.REVIEW_REQUIRED
    )
    release_allowed: bool = False
    runtime_catalog_registered: bool = False
    reviewer: str | None = None
    reviewed_at: str | None = None
    review_note: str | None = None

    @model_validator(mode="after")
    def _draft_starts_closed(self) -> DraftAssetEntry:
        if self.asset_lifecycle_status is not AssetLifecycleStatus.DRAFT:
            raise ValueError("new draft assets must start with lifecycle status draft")
        if self.visual_quality_status is not VisualQualityStatus.REVIEW_REQUIRED:
            raise ValueError("draft assets must start with visual_quality_status=review_required")
        if self.legal_review_status is not DecorativeAssetLegalReviewStatus.NOT_REVIEWED:
            raise ValueError("draft assets must start with legal_review_status=not_reviewed")
        if (
            self.printability_status
            is not DecorativeAssetPrintabilityStatus.REVIEW_REQUIRED
        ):
            raise ValueError("draft assets must start with printability_status=review_required")
        if self.release_allowed:
            raise ValueError("draft assets must start with release_allowed=false")
        if self.runtime_catalog_registered:
            raise ValueError("draft assets must not be runtime catalog registered")
        return self


class DraftAssetAuthoringRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_prompt_ko: str = Field(min_length=1)
    subject: str | None = None
    category: str | None = None
    style: str | None = None
    review_queue_reason: str = "no_verified_runtime_catalog_asset"


class DraftAssetAuthoringStatus(StrEnum):
    DRAFT_CREATED = "draft_created"
    DRAFT_REUSE_AVAILABLE = "draft_reuse_available"
    SIMILAR_REFERENCE_AVAILABLE = "similar_reference_available"
    ASK_USER = "ask_user"
    RUNTIME_CATALOG_AVAILABLE = "runtime_catalog_available"


class DraftAssetAuthoringResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: DraftAssetAuthoringStatus
    reason: str
    draft_asset: DraftAssetEntry | None = None
    runtime_asset_id: str | None = None
    intake_status: str | None = None
    intake_reason: str | None = None
    intake_new_draft_allowed: bool | None = None
    intake_metadata: dict[str, object] = Field(default_factory=dict)
    candidate_asset_ids: tuple[str, ...] = ()
    reference_candidate_ids: tuple[str, ...] = ()
    selected_route: NewbieRoute


class DraftAssetReviewRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    draft_asset_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_]*$")
    decision: DraftReviewDecision
    asset_lifecycle_status: AssetLifecycleStatus
    reviewer: str = Field(min_length=1)
    reviewed_at: str = Field(min_length=1)
    review_note: str = Field(min_length=1)
    required_next_gates: tuple[str, ...]
    release_allowed: bool = False
    runtime_catalog_registered: bool = False

    @model_validator(mode="after")
    def _review_record_does_not_release(self) -> DraftAssetReviewRecord:
        if self.release_allowed:
            raise ValueError("draft review records must not grant release")
        if self.runtime_catalog_registered:
            raise ValueError("draft review records must not auto-register runtime catalog")
        return self


class DraftAssetEvidenceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    draft_asset_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_]*$")
    evidence_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_]*$")
    evidence_type: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[A-Fa-f0-9]{64}$")
    size_bytes: int = Field(gt=0)
    metadata: dict[str, object] = Field(default_factory=dict)


__all__ = [
    "AssetLifecycleStatus",
    "DraftAssetAuthoringRequest",
    "DraftAssetAuthoringResult",
    "DraftAssetAuthoringStatus",
    "DraftAssetEntry",
    "DraftAssetEvidenceRecord",
    "DraftAssetReviewRecord",
    "DraftReviewDecision",
]
