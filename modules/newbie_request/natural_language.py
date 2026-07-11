from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from modules.newbie_request.asset_authoring import AssetAuthoringPipeline
from modules.newbie_request.asset_catalog import (
    AssetCandidateExplanation,
    DecorativeAssetSelectionRequest,
    DecorativeAssetSelectionStatus,
    DecorativeAssetSelector,
    load_decorative_asset_catalog,
)
from modules.newbie_request.asset_draft_schemas import (
    DraftAssetAuthoringRequest,
    DraftAssetAuthoringResult,
    DraftAssetAuthoringStatus,
)
from modules.newbie_request.asset_intake import (
    AssetIntakeReason,
    AssetIntakeStatus,
)
from modules.newbie_request.catalog import load_newbie_request_catalog
from modules.newbie_request.llm_schemas import LLMAssistShadowResult
from modules.newbie_request.route_selector import NewbieRouteSelector
from modules.newbie_request.schemas import (
    NewbieRoute,
    QualityPolicy,
    RouteSelectionRequest,
    SizeMM,
    VisualQualityStatus,
)
from modules.newbie_request.subject_aliases import (
    SubjectAliasRow,
    SubjectAliasTable,
    TrademarkBlockedSubject,
    TrademarkBlockedSubjectTable,
    load_subject_alias_table,
    load_trademark_blocked_subjects,
    normalize_alias_term,
)


class RequirementExtractionReason(StrEnum):
    EXACT_DECORATIVE_KEYRING_SUBJECT = "exact_decorative_keyring_subject"
    DRAFTABLE_DECORATIVE_KEYRING_SUBJECT = "draftable_decorative_keyring_subject"
    MISSING_MECHANICAL_DIMENSIONS = "missing_mechanical_dimensions"
    UNSUPPORTED_OR_AMBIGUOUS_ROUTE = "unsupported_or_ambiguous_route"
    AMBIGUOUS_DECORATIVE_SUBJECT = "ambiguous_decorative_subject"
    TRADEMARK_BLOCKED_SUBJECT = "trademark_blocked_subject"
    UNKNOWN_DECORATIVE_SUBJECT = "unknown_decorative_subject"


class RouteIntegrationReason(StrEnum):
    VERIFIED_RUNTIME_ASSET = "verified_runtime_asset"
    DRAFT_AUTHORING_REQUIRED = "draft_authoring_required"
    CLARIFICATION_REQUIRED = "clarification_required"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"


class NewbieRequirementExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_prompt_ko: str = Field(min_length=1)
    request_id: str | None = None
    category: str | None = None
    object_type: str | None = None
    subject: str | None = None
    style: str | None = None
    size_hint: SizeMM | None = None
    required_features: tuple[str, ...] = ()
    quality_policy: QualityPolicy
    clarification_required: bool
    clarification_reason: RequirementExtractionReason | None = None
    clarification_questions: tuple[str, ...] = ()
    confidence: float = Field(ge=0, le=1)
    reason: RequirementExtractionReason


class NaturalLanguageAssetIntakeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    runtime_asset_id: str | None = None
    draft_asset_id: str | None = None
    candidate_asset_ids: tuple[str, ...] = ()
    reference_candidate_ids: tuple[str, ...] = ()
    new_draft_allowed: bool
    metadata: dict[str, object] = Field(default_factory=dict)


class NaturalLanguageRouteResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement: NewbieRequirementExtraction
    selected_route: NewbieRoute
    reason: RouteIntegrationReason
    request_id: str | None = None
    runtime_asset_id: str | None = None
    draft_asset_id: str | None = None
    candidate_asset_ids: tuple[str, ...] = ()
    clarification_required: bool
    clarification_questions: tuple[str, ...] = ()
    generation_allowed: bool
    intake_decision: NaturalLanguageAssetIntakeDecision | None = None
    shadow_result: LLMAssistShadowResult | None = None


class LightweightRequirementExtractor:
    """Deterministic Phase 12Q extractor for representative newbie prompts."""

    def __init__(
        self,
        *,
        alias_table: SubjectAliasTable | None = None,
        trademark_table: TrademarkBlockedSubjectTable | None = None,
    ) -> None:
        self._alias_table = alias_table or load_subject_alias_table()
        self._trademark_table = trademark_table or load_trademark_blocked_subjects()

    def extract(self, user_prompt_ko: str) -> NewbieRequirementExtraction:
        prompt = normalize_alias_term(user_prompt_ko)
        trademark_match = self._trademark_table.match(user_prompt_ko)
        if trademark_match is not None:
            return _trademark_blocked(user_prompt_ko, trademark_match)

        alias_matches = self._alias_table.matches(user_prompt_ko)
        known_subjects = {
            alias.subject
            for alias in alias_matches
            if alias.subject is not None
            and alias.category == "decorative_keyring"
        }
        if _mentions_keyring(prompt) and len(known_subjects) > 1:
            return _clarification(
                user_prompt_ko,
                category="decorative_keyring",
                object_type="decorative_keyring",
                required_features=("recognizable_subject", "keyring_hole"),
                reason=RequirementExtractionReason.AMBIGUOUS_DECORATIVE_SUBJECT,
                questions=("하나의 동물 주제를 선택해 주세요.",),
            )

        for alias in alias_matches:
            if alias.category == "decorative_keyring":
                if alias.subject is None:
                    return _clarification_from_alias(user_prompt_ko, alias)
                if not _mentions_keyring(prompt):
                    return _clarification(
                        user_prompt_ko,
                        category=alias.category,
                        object_type=alias.object_type,
                        required_features=alias.required_features,
                        reason=RequirementExtractionReason.UNSUPPORTED_OR_AMBIGUOUS_ROUTE,
                        questions=("키링으로 만들지, 장식품으로 만들지 알려주세요.",),
                    )
                return _decorative_keyring_from_alias(user_prompt_ko, alias)
            return _clarification_from_alias(user_prompt_ko, alias)

        if _mentions_keyring(prompt):
            return _clarification(
                user_prompt_ko,
                category="decorative_keyring",
                object_type="decorative_keyring",
                required_features=("recognizable_subject", "keyring_hole"),
                reason=RequirementExtractionReason.UNKNOWN_DECORATIVE_SUBJECT,
                questions=("지원 가능한 동물 주제인지 확인이 필요합니다.",),
            )

        return _clarification(
            user_prompt_ko,
            category=None,
            object_type=None,
            required_features=(),
            reason=RequirementExtractionReason.UNSUPPORTED_OR_AMBIGUOUS_ROUTE,
            questions=("만들 물건의 종류와 대략적인 크기를 알려주세요.",),
        )


class NaturalLanguageRouteIntegrator:
    """Routes extracted beginner prompts without silent freeform fallback."""

    def __init__(
        self,
        *,
        extractor: LightweightRequirementExtractor | None = None,
        asset_selector: DecorativeAssetSelector | None = None,
        asset_authoring: AssetAuthoringPipeline | None = None,
    ) -> None:
        self._extractor = extractor or LightweightRequirementExtractor()
        catalog = load_newbie_request_catalog()
        self._route_selector = NewbieRouteSelector(catalog)
        self._asset_selector = asset_selector or DecorativeAssetSelector(
            load_decorative_asset_catalog()
        )
        self._asset_authoring = asset_authoring or AssetAuthoringPipeline(
            self._asset_selector
        )

    def route(self, user_prompt_ko: str) -> NaturalLanguageRouteResult:
        requirement = self._extractor.extract(user_prompt_ko)
        return self._route_extraction(user_prompt_ko, requirement)

    def route_if_recognized(
        self,
        user_prompt_ko: str,
    ) -> NaturalLanguageRouteResult | None:
        """Return a route only for prompts owned by the newbie router."""
        requirement = self._extractor.extract(user_prompt_ko)
        if requirement.category is None:
            return None
        return self._route_extraction(user_prompt_ko, requirement)

    def _route_extraction(
        self,
        user_prompt_ko: str,
        requirement: NewbieRequirementExtraction,
    ) -> NaturalLanguageRouteResult:
        if requirement.clarification_required:
            route_reason = RouteIntegrationReason.CLARIFICATION_REQUIRED
            intake_decision = None
            if (
                requirement.clarification_reason
                is RequirementExtractionReason.TRADEMARK_BLOCKED_SUBJECT
            ):
                route_reason = RouteIntegrationReason.MANUAL_REVIEW_REQUIRED
                intake_decision = NaturalLanguageAssetIntakeDecision(
                    status="trademark_blocked",
                    reason="trademark_or_ip_review_required",
                    new_draft_allowed=False,
                    metadata={
                        "subject": requirement.subject,
                        "category": requirement.category,
                        "deterministic_guard": "trademark_blocked_subject",
                    },
                )
            return NaturalLanguageRouteResult(
                requirement=requirement,
                selected_route=NewbieRoute.ASK_USER,
                reason=route_reason,
                request_id=requirement.request_id,
                clarification_required=True,
                clarification_questions=requirement.clarification_questions,
                generation_allowed=False,
                intake_decision=intake_decision,
            )

        route_result = self._route_selector.select(
            RouteSelectionRequest(
                request_id=requirement.request_id,
                user_prompt_ko=user_prompt_ko,
                category=requirement.category,
                object_type=requirement.object_type,
            )
        )
        asset_selection = self._asset_selector.select(
            DecorativeAssetSelectionRequest(
                request_id=route_result.request_id,
                user_prompt_ko=user_prompt_ko,
                subject=requirement.subject,
                style=requirement.style,
            )
        )
        if asset_selection.status is DecorativeAssetSelectionStatus.SELECTED:
            return NaturalLanguageRouteResult(
                requirement=requirement,
                selected_route=NewbieRoute.CURATED_ASSET,
                reason=RouteIntegrationReason.VERIFIED_RUNTIME_ASSET,
                request_id=route_result.request_id,
                runtime_asset_id=asset_selection.asset_id,
                candidate_asset_ids=asset_selection.candidate_asset_ids,
                clarification_required=False,
                generation_allowed=True,
                intake_decision=NaturalLanguageAssetIntakeDecision(
                    status=AssetIntakeStatus.RUNTIME_CATALOG_MATCH.value,
                    reason=AssetIntakeReason.RUNTIME_EXACT_MATCH.value,
                    runtime_asset_id=asset_selection.asset_id,
                    candidate_asset_ids=asset_selection.candidate_asset_ids,
                    new_draft_allowed=False,
                    metadata=_metadata_with_candidate_explanations(
                        {
                            "request_id": asset_selection.request_id,
                            "subject": asset_selection.subject,
                            "category": asset_selection.category,
                            "style": asset_selection.style,
                        },
                        asset_selection.candidate_explanations,
                    ),
                ),
            )

        draft_result = self._asset_authoring.handle(
            DraftAssetAuthoringRequest(
                user_prompt_ko=user_prompt_ko,
                subject=requirement.subject,
                category=(
                    "keyring"
                    if requirement.category == "decorative_keyring"
                    else None
                ),
                style=requirement.style,
            )
        )
        if draft_result.status in {
            DraftAssetAuthoringStatus.DRAFT_CREATED,
            DraftAssetAuthoringStatus.DRAFT_REUSE_AVAILABLE,
        }:
            return NaturalLanguageRouteResult(
                requirement=requirement,
                selected_route=NewbieRoute.DRAFT_ASSET,
                reason=RouteIntegrationReason.DRAFT_AUTHORING_REQUIRED,
                draft_asset_id=draft_result.draft_asset.draft_asset_id
                if draft_result.draft_asset is not None
                else None,
                clarification_required=False,
                generation_allowed=False,
                intake_decision=_intake_decision_from_authoring(draft_result),
            )

        if draft_result.status is DraftAssetAuthoringStatus.SIMILAR_REFERENCE_AVAILABLE:
            return NaturalLanguageRouteResult(
                requirement=requirement,
                selected_route=NewbieRoute.ASK_USER,
                reason=RouteIntegrationReason.MANUAL_REVIEW_REQUIRED,
                request_id=route_result.request_id,
                clarification_required=True,
                clarification_questions=(
                    "유사한 에셋 후보를 재사용하거나 수정할지 검토해 주세요.",
                ),
                generation_allowed=False,
                intake_decision=_intake_decision_from_authoring(draft_result),
            )

        return NaturalLanguageRouteResult(
            requirement=requirement,
            selected_route=NewbieRoute.ASK_USER,
            reason=RouteIntegrationReason.MANUAL_REVIEW_REQUIRED,
            request_id=route_result.request_id,
            clarification_required=True,
            clarification_questions=("지원 가능한 템플릿 또는 에셋 경로를 선택해 주세요.",),
            generation_allowed=False,
            intake_decision=_intake_decision_from_authoring(draft_result),
        )


def _decorative_keyring(
    prompt: str,
    *,
    request_id: str | None,
    subject: str,
    required_features: tuple[str, ...],
    reason: RequirementExtractionReason,
    confidence: float,
    style: str | None = None,
) -> NewbieRequirementExtraction:
    return NewbieRequirementExtraction(
        user_prompt_ko=prompt,
        request_id=request_id,
        category="decorative_keyring",
        object_type="decorative_keyring",
        subject=subject,
        style=style,
        size_hint=None,
        required_features=required_features,
        quality_policy=QualityPolicy(
            visual_quality_required=True,
            manual_review_required=True,
            visual_quality_status=VisualQualityStatus.REVIEW_REQUIRED,
        ),
        clarification_required=False,
        confidence=confidence,
        reason=reason,
    )


def _decorative_keyring_from_alias(
    prompt: str,
    alias: SubjectAliasRow,
) -> NewbieRequirementExtraction:
    reason = RequirementExtractionReason(alias.route_reason)
    return _decorative_keyring(
        prompt,
        request_id=alias.request_id,
        subject=alias.subject or "",
        style=alias.style,
        required_features=alias.required_features,
        reason=reason,
        confidence=alias.confidence,
    )


def _clarification(
    prompt: str,
    *,
    category: str | None,
    object_type: str | None,
    required_features: tuple[str, ...],
    reason: RequirementExtractionReason,
    questions: tuple[str, ...],
) -> NewbieRequirementExtraction:
    return NewbieRequirementExtraction(
        user_prompt_ko=prompt,
        category=category,
        object_type=object_type,
        required_features=required_features,
        quality_policy=QualityPolicy(
            visual_quality_required=category == "decorative_keyring",
            manual_review_required=category == "decorative_keyring",
            visual_quality_status=VisualQualityStatus.REVIEW_REQUIRED
            if category == "decorative_keyring"
            else VisualQualityStatus.NOT_EVALUATED,
        ),
        clarification_required=True,
        clarification_reason=reason,
        clarification_questions=questions,
        confidence=0.55,
        reason=reason,
    )


def _clarification_from_alias(
    prompt: str,
    alias: SubjectAliasRow,
) -> NewbieRequirementExtraction:
    return _clarification(
        prompt,
        category=alias.category,
        object_type=alias.object_type,
        required_features=alias.required_features,
        reason=RequirementExtractionReason(alias.route_reason),
        questions=alias.questions,
    )


def _trademark_blocked(
    prompt: str,
    blocked: TrademarkBlockedSubject,
) -> NewbieRequirementExtraction:
    return _clarification(
        prompt,
        category=blocked.category,
        object_type="decorative_keyring"
        if blocked.category == "decorative_keyring"
        else None,
        required_features=("rights_clearance", "user_owned_design"),
        reason=RequirementExtractionReason.TRADEMARK_BLOCKED_SUBJECT,
        questions=blocked.questions,
    ).model_copy(
        update={
            "subject": blocked.subject,
            "style": blocked.style,
            "confidence": 0.99,
        }
    )


def _mentions_keyring(prompt: str) -> bool:
    return "키링" in prompt or "열쇠고리" in prompt or "keyring" in prompt


def _intake_decision_from_authoring(
    result: DraftAssetAuthoringResult,
) -> NaturalLanguageAssetIntakeDecision | None:
    if result.intake_status is None or result.intake_reason is None:
        return None
    return NaturalLanguageAssetIntakeDecision(
        status=result.intake_status,
        reason=result.intake_reason,
        runtime_asset_id=result.runtime_asset_id,
        draft_asset_id=result.draft_asset.draft_asset_id
        if result.draft_asset is not None
        else None,
        candidate_asset_ids=result.candidate_asset_ids,
        reference_candidate_ids=result.reference_candidate_ids,
        new_draft_allowed=result.intake_new_draft_allowed is True,
        metadata=result.intake_metadata,
    )


def _metadata_with_candidate_explanations(
    metadata: dict[str, object],
    explanations: tuple[AssetCandidateExplanation, ...],
) -> dict[str, object]:
    if not explanations:
        return metadata
    return {
        **metadata,
        "candidate_explanations": [
            explanation.model_dump(mode="json") for explanation in explanations
        ],
    }


__all__ = [
    "LightweightRequirementExtractor",
    "NaturalLanguageAssetIntakeDecision",
    "NaturalLanguageRouteIntegrator",
    "NaturalLanguageRouteResult",
    "NewbieRequirementExtraction",
    "RequirementExtractionReason",
    "RouteIntegrationReason",
]
