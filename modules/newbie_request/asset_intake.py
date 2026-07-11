from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from modules.newbie_request.asset_catalog import (
    AssetCandidateExplanation,
    AssetCandidateExplanationSource,
    AssetCandidateMatchLevel,
    DecorativeAssetSelectionRequest,
    DecorativeAssetSelectionStatus,
    DecorativeAssetSelector,
    DecorativeAssetSourceType,
)
from modules.newbie_request.asset_draft_schemas import DraftAssetEntry
from modules.newbie_request.draft_queue import DraftReviewQueue

_SAFE_PUBLIC_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")


class AssetIntakeError(ValueError):
    """Raised when asset intake receives unsafe identifiers or paths."""


class AssetIntakeStatus(StrEnum):
    RUNTIME_CATALOG_MATCH = "runtime_catalog_match"
    DRAFT_QUEUE_MATCH = "draft_queue_match"
    DUPLICATE_OR_SIMILAR_CANDIDATE = "duplicate_or_similar_candidate"
    NEW_DRAFT_ALLOWED = "new_draft_allowed"


class AssetIntakeReason(StrEnum):
    RUNTIME_EXACT_MATCH = "runtime_exact_match"
    EXISTING_DRAFT_MATCH = "existing_draft_match"
    SIMILAR_REFERENCE_CANDIDATE = "similar_reference_candidate"
    NO_EXISTING_ASSET_OR_DRAFT = "no_existing_asset_or_draft"


class ReferenceAssetCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reference_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_]*$")
    subject: str = Field(min_length=1)
    category: str = Field(min_length=1)
    style: str | None = None
    source_type: DecorativeAssetSourceType
    license: str = Field(min_length=1)
    source_url_or_owner: str = Field(min_length=1)
    provenance_note: str = Field(min_length=1)
    commercial_allowed: bool
    redistribution_allowed: bool
    modification_allowed: bool

    @model_validator(mode="after")
    def _reference_must_be_rights_clear(self) -> ReferenceAssetCandidate:
        if not (
            self.commercial_allowed
            and self.redistribution_allowed
            and self.modification_allowed
        ):
            raise ValueError("reference candidates must have rights metadata cleared")
        return self


class AssetIntakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_prompt_ko: str = Field(min_length=1)
    request_id: str | None = None
    subject: str | None = None
    category: str | None = None
    style: str | None = None


class AssetIntakeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: AssetIntakeStatus
    reason: AssetIntakeReason
    runtime_asset_id: str | None = None
    draft_asset_id: str | None = None
    draft_asset: DraftAssetEntry | None = None
    candidate_asset_ids: tuple[str, ...] = ()
    reference_candidate_ids: tuple[str, ...] = ()
    new_draft_allowed: bool
    metadata: dict[str, object] = Field(default_factory=dict)
    candidate_explanations: tuple[AssetCandidateExplanation, ...] = ()


class AssetFolderPolicy:
    """Path policy for future asset library storage.

    This class only computes safe paths. It does not create folders and does not
    mutate the runtime catalog.
    """

    def __init__(self, asset_root: str | Path) -> None:
        self.asset_root = Path(asset_root).resolve()

    def runtime_asset_path(self, asset_id: str) -> Path:
        return self._path_under_root("runtime", _safe_public_id(asset_id), "asset.json")

    def draft_asset_path(self, draft_asset_id: str) -> Path:
        return self._path_under_root(
            "drafts",
            _safe_public_id(draft_asset_id),
            "draft_asset.json",
        )

    def draft_review_path(self, draft_asset_id: str, review_id: str) -> Path:
        return self._path_under_root(
            "drafts",
            _safe_public_id(draft_asset_id),
            "reviews",
            f"{_safe_public_id(review_id)}.json",
        )

    def reference_note_path(self, reference_id: str) -> Path:
        return self._path_under_root(
            "references",
            _safe_public_id(reference_id),
            "reference_note.json",
        )

    def _path_under_root(self, *parts: str) -> Path:
        path = (self.asset_root / Path(*parts)).resolve()
        if not path.is_relative_to(self.asset_root):
            raise AssetIntakeError(f"Asset folder path escapes root: {path}")
        return path


class AssetIntakeResolver:
    """Deterministic intake gate before creating a new draft asset."""

    def __init__(
        self,
        runtime_selector: DecorativeAssetSelector,
        *,
        draft_queue: DraftReviewQueue | None = None,
        reference_candidates: tuple[ReferenceAssetCandidate, ...] = (),
    ) -> None:
        self._runtime_selector = runtime_selector
        self._draft_queue = draft_queue
        self._reference_candidates = reference_candidates

    def resolve(self, request: AssetIntakeRequest) -> AssetIntakeResult:
        runtime_selection = self._runtime_selector.select(
            DecorativeAssetSelectionRequest(
                request_id=request.request_id,
                user_prompt_ko=request.user_prompt_ko,
                subject=request.subject,
                category=request.category,
                style=request.style,
            )
        )
        if runtime_selection.status is DecorativeAssetSelectionStatus.SELECTED:
            return AssetIntakeResult(
                status=AssetIntakeStatus.RUNTIME_CATALOG_MATCH,
                reason=AssetIntakeReason.RUNTIME_EXACT_MATCH,
                runtime_asset_id=runtime_selection.asset_id,
                candidate_asset_ids=runtime_selection.candidate_asset_ids,
                new_draft_allowed=False,
                candidate_explanations=runtime_selection.candidate_explanations,
                metadata={
                    "request_id": runtime_selection.request_id,
                    "subject": runtime_selection.subject,
                    "category": runtime_selection.category,
                    "style": runtime_selection.style,
                },
            )

        draft = self._matching_draft(request)
        if draft is not None:
            return AssetIntakeResult(
                status=AssetIntakeStatus.DRAFT_QUEUE_MATCH,
                reason=AssetIntakeReason.EXISTING_DRAFT_MATCH,
                draft_asset_id=draft.draft_asset_id,
                draft_asset=draft,
                new_draft_allowed=False,
                candidate_explanations=(
                    _draft_queue_explanation(draft),
                ),
                metadata={
                    "subject": draft.subject,
                    "category": draft.category,
                    "style": draft.style,
                    "asset_lifecycle_status": draft.asset_lifecycle_status.value,
                },
            )

        reference_candidates = self._matching_references(request)
        if reference_candidates:
            return AssetIntakeResult(
                status=AssetIntakeStatus.DUPLICATE_OR_SIMILAR_CANDIDATE,
                reason=AssetIntakeReason.SIMILAR_REFERENCE_CANDIDATE,
                reference_candidate_ids=tuple(
                    candidate.reference_id for candidate in reference_candidates
                ),
                new_draft_allowed=False,
                candidate_explanations=_reference_candidate_explanations(
                    reference_candidates
                ),
                metadata={
                    "subject": request.subject,
                    "category": request.category,
                    "style": request.style,
                },
            )

        return AssetIntakeResult(
            status=AssetIntakeStatus.NEW_DRAFT_ALLOWED,
            reason=AssetIntakeReason.NO_EXISTING_ASSET_OR_DRAFT,
            new_draft_allowed=True,
            candidate_explanations=(
                _new_draft_explanation(request),
            ),
            metadata={
                "subject": request.subject,
                "category": request.category,
                "style": request.style,
            },
        )

    def _matching_draft(self, request: AssetIntakeRequest) -> DraftAssetEntry | None:
        if self._draft_queue is None:
            return None
        matches = [
            draft
            for draft in self._draft_queue.list_drafts()
            if _same_identity(
                subject=draft.subject,
                category=draft.category,
                style=draft.style,
                request=request,
            )
        ]
        return matches[0] if matches else None

    def _matching_references(
        self,
        request: AssetIntakeRequest,
    ) -> tuple[ReferenceAssetCandidate, ...]:
        return tuple(
            candidate
            for candidate in self._reference_candidates
            if _same_identity(
                subject=candidate.subject,
                category=candidate.category,
                style=candidate.style,
                request=request,
            )
        )


def _same_identity(
    *,
    subject: str,
    category: str,
    style: str | None,
    request: AssetIntakeRequest,
) -> bool:
    if request.subject is not None and subject != request.subject:
        return False
    if request.category is not None and category != request.category:
        return False
    if request.subject is None or request.category is None:
        return False
    if request.style is None:
        return True
    return style == request.style


def _draft_queue_explanation(draft: DraftAssetEntry) -> AssetCandidateExplanation:
    return AssetCandidateExplanation(
        candidate_id=draft.draft_asset_id,
        asset_id=None,
        source=AssetCandidateExplanationSource.DRAFT_QUEUE,
        match_level=AssetCandidateMatchLevel.NOT_APPLICABLE,
        subject=draft.subject,
        category=draft.category,
        style=draft.style,
        reason_code=AssetIntakeReason.EXISTING_DRAFT_MATCH.value,
        display_text_ko="이미 작성 중인 초안이 있어 새 초안을 만들지 않습니다.",
    )


def _reference_candidate_explanations(
    candidates: tuple[ReferenceAssetCandidate, ...],
) -> tuple[AssetCandidateExplanation, ...]:
    return tuple(
        AssetCandidateExplanation(
            candidate_id=candidate.reference_id,
            asset_id=None,
            source=AssetCandidateExplanationSource.REFERENCE_CANDIDATE,
            match_level=AssetCandidateMatchLevel.NOT_APPLICABLE,
            subject=candidate.subject,
            category=candidate.category,
            style=candidate.style,
            reason_code=AssetIntakeReason.SIMILAR_REFERENCE_CANDIDATE.value,
            display_text_ko="유사한 참고 후보가 있어 새 에셋 생성 전에 검토가 필요합니다.",
        )
        for candidate in candidates
    )


def _new_draft_explanation(request: AssetIntakeRequest) -> AssetCandidateExplanation:
    return AssetCandidateExplanation(
        candidate_id=_new_draft_candidate_id(request),
        asset_id=None,
        source=AssetCandidateExplanationSource.NEW_DRAFT,
        match_level=AssetCandidateMatchLevel.NOT_APPLICABLE,
        subject=request.subject,
        category=request.category,
        style=request.style,
        reason_code=AssetIntakeReason.NO_EXISTING_ASSET_OR_DRAFT.value,
        display_text_ko=(
            "기존 에셋, 초안, 참고 후보가 없어 초안 생성 경계로 진행할 수 있습니다. "
            "검토 전에는 제품 후보가 아닙니다."
        ),
    )


def _new_draft_candidate_id(request: AssetIntakeRequest) -> str:
    if request.request_id is not None:
        return request.request_id
    parts = [request.subject, request.category, request.style]
    candidate_id = "_".join(part for part in parts if part)
    if candidate_id:
        return f"{candidate_id}_new_draft"
    digest = hashlib.sha256(request.user_prompt_ko.encode("utf-8")).hexdigest()[:12]
    return f"request_{digest}"


def _safe_public_id(value: str) -> str:
    if not _SAFE_PUBLIC_ID_RE.fullmatch(value):
        raise AssetIntakeError(f"Invalid public-safe asset id: {value!r}")
    return value


__all__ = [
    "AssetFolderPolicy",
    "AssetIntakeError",
    "AssetIntakeReason",
    "AssetIntakeRequest",
    "AssetIntakeResolver",
    "AssetIntakeResult",
    "AssetIntakeStatus",
    "ReferenceAssetCandidate",
]
