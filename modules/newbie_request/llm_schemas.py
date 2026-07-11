"""Strict schemas for the Phase 12Z-A local LLM assist shadow layer.

These contracts are advisory only. They never carry routing, generation,
release, legal, or visual authority. A shadow result records what a local
LLM *would* have proposed next to the deterministic product decision so the
two can be compared offline.

Hard rules enforced here:

- ``used_for_decision`` must always be ``False``;
- only ``LLMAssistMode.SHADOW`` is supported in this phase;
- a successful result must carry a validated ``requirement_candidate`` and no
  ``fallback_reason``;
- a failed result must carry a ``fallback_reason`` and no candidate, so an
  empty/timeout/invalid response can never masquerade as success.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from modules.newbie_request.asset_catalog import DecorativeAssetLegalReviewStatus
from modules.newbie_request.asset_registration import RegistrationGateStatus
from modules.newbie_request.printability import PrintabilityReport
from modules.newbie_request.schemas import VisualQualityStatus

__all__ = [
    "DRAFT_SPEC_SIZE_HINT_MAX_MM",
    "DraftSpecProposal",
    "DraftSpecProposalSource",
    "DraftSpecSizeHintMM",
    "DraftSpecShadowSkipReason",
    "FailureRepairAdvice",
    "FailureRepairInput",
    "FailureRepairShadowResult",
    "FailureRepairStage",
    "FailureRepairSuggestedNextAction",
    "HumanReviewSummary",
    "HumanReviewSummaryInput",
    "HumanReviewSummaryShadowResult",
    "LLM_ASSIST_SCHEMA_VERSION",
    "LLMAdvisoryCandidateRoute",
    "LLMAssistContext",
    "LLMAssistFailureReason",
    "LLMAssistMode",
    "LLMDraftSpecShadowResult",
    "LLMAssistShadowResult",
    "LLMRequirementCandidate",
    "LLMSearchExpansionCandidate",
    "compute_prompt_hash",
    "human_review_text_has_unsafe_approval_assertion",
    "human_review_summary_has_unsafe_approval_assertion",
]

LLM_ASSIST_SCHEMA_VERSION = "12z-a.1"
DRAFT_SPEC_SIZE_HINT_MAX_MM = 200.0


class LLMAssistMode(StrEnum):
    """Operating mode for the local LLM assist layer."""

    SHADOW = "shadow"


class LLMAssistFailureReason(StrEnum):
    """Why a shadow attempt did not produce a validated candidate."""

    PROVIDER_NOT_CONFIGURED = "provider_not_configured"
    TIMEOUT = "timeout"
    EMPTY_RESPONSE = "empty_response"
    SCHEMA_INVALID = "schema_invalid"
    PROVIDER_ERROR = "provider_error"


class LLMAdvisoryCandidateRoute(StrEnum):
    """Advisory route labels that never authorize product behavior."""

    CANDIDATE_SUBJECT_PROPOSED = "candidate_subject_proposed"


class LLMAssistContext(BaseModel):
    """Deterministic catalog snapshot used for prompting and hashing.

    The context is repo-local data only. It exists so the same prompt against
    the same catalog snapshot produces a stable ``prompt_hash``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    known_subjects: tuple[str, ...] = ()
    known_categories: tuple[str, ...] = ()
    known_styles: tuple[str, ...] = ()
    catalog_snapshot_id: str | None = None


class LLMRequirementCandidate(BaseModel):
    """Advisory structured interpretation of a beginner prompt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    category: str | None = None
    object_type: str | None = None
    subject_candidates: tuple[str, ...] = ()
    style: str | None = None
    required_features: tuple[str, ...] = ()
    clarification_required: bool = False
    clarification_question: str | None = None
    confidence: float = Field(ge=0, le=1)


class LLMSearchExpansionCandidate(BaseModel):
    """Advisory normalized search terms for asset/catalog discovery."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject_terms: tuple[str, ...] = ()
    style_terms: tuple[str, ...] = ()
    category_terms: tuple[str, ...] = ()


class DraftSpecProposalSource(StrEnum):
    """Where a non-authoritative draft spec proposal came from."""

    LOCAL_LLM_SHADOW = "local_llm_shadow"
    FIXTURE_BASELINE = "fixture_baseline"


class DraftSpecShadowSkipReason(StrEnum):
    """Why product wiring must skip draft-spec shadow calls."""

    ROUTE_NOT_NEW_DRAFT_ALLOWED = "route_not_new_draft_allowed"
    TRADEMARK_OR_MANUAL_REVIEW_BOUNDARY = (
        "trademark_or_manual_review_boundary"
    )
    UNKNOWN_SUBJECT = "unknown_subject"
    RUNTIME_ASSET_ALREADY_AVAILABLE = "runtime_asset_already_available"
    DRAFT_OR_REFERENCE_REUSE_AVAILABLE = "draft_or_reference_reuse_available"
    ASK_USER_REQUIRES_CLARIFICATION = "ask_user_requires_clarification"


class FailureRepairStage(StrEnum):
    """Failure boundary that produced repair-advice input."""

    SEMANTIC_VALIDATION = "semantic_validation"
    PRINTABILITY = "printability"
    VISUAL_QUALITY = "visual_quality"
    ROUTE_SELECTION = "route_selection"
    MANUAL_REVIEW = "manual_review"


class FailureRepairSuggestedNextAction(StrEnum):
    """Advisory next action labels that never execute retries."""

    ADD_MISSING_FEATURES = "add_missing_features"
    PROVIDE_DIMENSIONS = "provide_dimensions"
    CHOOSE_SUPPORTED_SUBJECT = "choose_supported_subject"
    REQUIRES_MANUAL_REVIEW = "requires_manual_review"


_UNSAFE_HUMAN_REVIEW_ASSERTION_TERMS = (
    "출시 가능합니다",
    "출시 가능 상태",
    "출시 승인",
    "출시해도 됩니다",
    "release_allowed=true",
    "release allowed",
    "release-ready",
    "release ready",
    "product_ready=true",
    "제품 준비 완료",
    "제품 품질 통과",
    "최종 통과",
    "최종 승인",
    "법무 승인되었습니다",
    "법적 승인되었습니다",
    "legal approved",
    "legal clearance",
    "상업 사용 승인되었습니다",
    "시각 품질 통과",
    "visual pass",
    "printability pass",
)


class FailureRepairInput(BaseModel):
    """Typed failure facts for advisory repair suggestions.

    This model stores existing subsystem taxonomy values. It must not carry raw
    exceptions or grant retry/execution authority.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    failure_stage: FailureRepairStage
    deterministic_reason: str = Field(min_length=1)
    semantic_missing_features: tuple[str, ...] = ()
    semantic_violated_constraints: tuple[str, ...] = ()
    printability_status: str | None = None
    printability_issue_codes: tuple[str, ...] = ()
    visual_quality_status: str | None = None
    route_reason: str | None = None
    manual_review_required: bool = False
    legal_review_required: bool = False


class FailureRepairAdvice(BaseModel):
    """Advisory repair suggestion, not a retry executor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    failure_stage: FailureRepairStage
    deterministic_reason: str = Field(min_length=1)
    user_facing_summary_ko: str = Field(min_length=1)
    suggested_next_action: FailureRepairSuggestedNextAction
    missing_information: tuple[str, ...] = ()
    retry_prompt_hint_ko: str | None = None
    risk_flags: tuple[str, ...] = ()
    used_for_decision: bool = False

    @model_validator(mode="after")
    def _enforce_advisory_invariants(self) -> FailureRepairAdvice:
        if self.used_for_decision:
            raise ValueError(
                "failure repair advice must never be used for product decisions"
            )
        return self


class HumanReviewSummaryInput(BaseModel):
    """Facts that may be summarized for a human reviewer.

    This model only mirrors existing gate taxonomy. It does not redefine
    visual, legal, printability, registration, or release authority.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    review_context: str = Field(min_length=1)
    visual_quality_status: VisualQualityStatus | None = None
    legal_review_status: DecorativeAssetLegalReviewStatus | None = None
    registration_gate_status: RegistrationGateStatus | None = None
    printability_report: PrintabilityReport | None = None
    evidence_refs: tuple[str, ...] = ()
    review_notes: tuple[str, ...] = ()
    risk_flags: tuple[str, ...] = ()


class HumanReviewSummary(BaseModel):
    """Advisory summary for human review preparation.

    The checklist and questions are framing aids only. They are never a visual
    pass, legal clearance, printability approval, release approval, or product
    decision.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    review_context: str = Field(min_length=1)
    evidence_summary_ko: str = Field(min_length=1)
    checklist_ko: tuple[str, ...] = Field(min_length=1)
    risk_notes_ko: tuple[str, ...] = ()
    suggested_human_questions_ko: tuple[str, ...] = ()
    used_for_decision: bool = False

    @model_validator(mode="after")
    def _enforce_human_review_summary_invariants(self) -> HumanReviewSummary:
        if self.used_for_decision:
            raise ValueError(
                "human review summary must never be used for product decisions"
            )
        if human_review_summary_has_unsafe_approval_assertion(self):
            raise ValueError(
                "human review summary must not assert approval, pass, or release"
            )
        return self


class DraftSpecSizeHintMM(BaseModel):
    """Approximate draft size hint in millimeters.

    Values above ``DRAFT_SPEC_SIZE_HINT_MAX_MM`` are allowed only as a measured
    risk signal. They must not grant generation or product authority.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    width: float = Field(gt=0)
    depth: float = Field(gt=0)
    height: float = Field(gt=0)

    @property
    def exceeds_policy_cap(self) -> bool:
        return any(
            value > DRAFT_SPEC_SIZE_HINT_MAX_MM
            for value in (self.width, self.depth, self.height)
        )


class DraftSpecProposal(BaseModel):
    """Advisory draft spec proposal, never a draft asset authority.

    This schema intentionally does not include legal, release, provenance, or
    runtime-catalog fields. It cannot be auto-converted to ``DraftAssetEntry``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str = Field(min_length=1)
    category: str = Field(min_length=1)
    style: str | None = None
    required_features: tuple[str, ...] = Field(min_length=1)
    size_hint_mm: DraftSpecSizeHintMM
    visual_intent_ko: str = Field(min_length=1)
    risk_flags: tuple[str, ...] = ()
    confidence: float = Field(ge=0, le=1)
    source: DraftSpecProposalSource

    @model_validator(mode="before")
    @classmethod
    def _add_size_risk_flag(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        size = data.get("size_hint_mm")
        if not isinstance(size, dict):
            return data
        values = (size.get("width"), size.get("depth"), size.get("height"))
        numeric_values: list[float] = []
        for value in values:
            if not isinstance(value, int | float):
                return data
            numeric_values.append(float(value))
        if not any(value > DRAFT_SPEC_SIZE_HINT_MAX_MM for value in numeric_values):
            return data
        raw_risk_flags = data.get("risk_flags")
        if raw_risk_flags is None:
            risk_flags: tuple[str, ...] = ()
        elif isinstance(raw_risk_flags, list | tuple):
            risk_flags = tuple(raw_risk_flags)
        else:
            return data
        if "size_hint_out_of_bounds" in risk_flags:
            return data
        return {
            **data,
            "risk_flags": (*risk_flags, "size_hint_out_of_bounds"),
        }


class LLMAssistShadowResult(BaseModel):
    """Recorded comparison between deterministic route and LLM candidate.

    Postconditions:
        ``used_for_decision`` is always ``False``. ``deterministic_route`` is
        the actual product route value and is never overwritten by LLM output.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: LLMAssistMode = LLMAssistMode.SHADOW
    succeeded: bool
    used_for_decision: bool = False
    model_name: str = Field(min_length=1)
    provider_name: str = Field(min_length=1)
    prompt_hash: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    deterministic_route: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    fallback_reason: LLMAssistFailureReason | None = None
    candidate_route: str | None = None
    candidate_subjects: tuple[str, ...] = ()
    candidate_style: str | None = None
    clarification_question: str | None = None
    requirement_candidate: LLMRequirementCandidate | None = None
    search_expansion: LLMSearchExpansionCandidate | None = None
    raw_response_excerpt: str | None = None

    @model_validator(mode="after")
    def _enforce_shadow_invariants(self) -> LLMAssistShadowResult:
        if self.used_for_decision:
            raise ValueError(
                "shadow result must never be used for product decisions"
            )
        if self.mode is not LLMAssistMode.SHADOW:
            raise ValueError("only shadow mode is supported in phase 12z-a")
        if self.succeeded:
            if self.fallback_reason is not None:
                raise ValueError(
                    "successful shadow result must not carry a fallback_reason"
                )
            if self.requirement_candidate is None:
                raise ValueError(
                    "successful shadow result requires a requirement_candidate"
                )
        else:
            if self.fallback_reason is None:
                raise ValueError(
                    "failed shadow result requires a fallback_reason"
                )
            if self.requirement_candidate is not None:
                raise ValueError(
                    "failed shadow result must not carry a requirement_candidate"
                )
        return self


class LLMDraftSpecShadowResult(BaseModel):
    """Recorded draft spec proposal next to deterministic product behavior.

    Postconditions:
        ``used_for_decision`` is always ``False``. A successful result only
        proves schema validity of an advisory proposal.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: LLMAssistMode = LLMAssistMode.SHADOW
    succeeded: bool
    used_for_decision: bool = False
    model_name: str = Field(min_length=1)
    provider_name: str = Field(min_length=1)
    prompt_hash: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    deterministic_route: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    fallback_reason: LLMAssistFailureReason | None = None
    proposal: DraftSpecProposal | None = None
    raw_response_excerpt: str | None = None

    @model_validator(mode="after")
    def _enforce_draft_spec_shadow_invariants(self) -> LLMDraftSpecShadowResult:
        if self.used_for_decision:
            raise ValueError(
                "draft spec shadow result must never be used for product decisions"
            )
        if self.mode is not LLMAssistMode.SHADOW:
            raise ValueError("only shadow mode is supported for draft spec proposal")
        if self.succeeded:
            if self.fallback_reason is not None:
                raise ValueError(
                    "successful draft spec shadow result must not carry fallback_reason"
                )
            if self.proposal is None:
                raise ValueError(
                    "successful draft spec shadow result requires a proposal"
                )
        else:
            if self.fallback_reason is None:
                raise ValueError(
                    "failed draft spec shadow result requires a fallback_reason"
                )
            if self.proposal is not None:
                raise ValueError(
                    "failed draft spec shadow result must not carry a proposal"
                )
        return self


class FailureRepairShadowResult(BaseModel):
    """Recorded failure-repair advice next to deterministic product behavior."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: LLMAssistMode = LLMAssistMode.SHADOW
    succeeded: bool
    used_for_decision: bool = False
    model_name: str = Field(min_length=1)
    provider_name: str = Field(min_length=1)
    prompt_hash: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    deterministic_route: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    fallback_reason: LLMAssistFailureReason | None = None
    failure_input: FailureRepairInput
    advice: FailureRepairAdvice | None = None
    raw_response_excerpt: str | None = None

    @model_validator(mode="after")
    def _enforce_failure_repair_shadow_invariants(self) -> FailureRepairShadowResult:
        if self.used_for_decision:
            raise ValueError(
                "failure repair shadow result must never be used for product decisions"
            )
        if self.mode is not LLMAssistMode.SHADOW:
            raise ValueError("only shadow mode is supported for failure repair advice")
        if self.succeeded:
            if self.fallback_reason is not None:
                raise ValueError(
                    "successful failure repair shadow result must not carry fallback_reason"
                )
            if self.advice is None:
                raise ValueError(
                    "successful failure repair shadow result requires advice"
                )
        else:
            if self.fallback_reason is None:
                raise ValueError(
                    "failed failure repair shadow result requires fallback_reason"
                )
            if self.advice is not None:
                raise ValueError(
                    "failed failure repair shadow result must not carry advice"
                )
        return self


class HumanReviewSummaryShadowResult(BaseModel):
    """Recorded human-review summary next to deterministic product state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: LLMAssistMode = LLMAssistMode.SHADOW
    succeeded: bool
    used_for_decision: bool = False
    model_name: str = Field(min_length=1)
    provider_name: str = Field(min_length=1)
    prompt_hash: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    deterministic_route: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    fallback_reason: LLMAssistFailureReason | None = None
    review_input: HumanReviewSummaryInput
    summary: HumanReviewSummary | None = None
    raw_response_excerpt: str | None = None

    @model_validator(mode="after")
    def _enforce_human_review_shadow_invariants(
        self,
    ) -> HumanReviewSummaryShadowResult:
        if self.used_for_decision:
            raise ValueError(
                "human review summary shadow result must never be used for decisions"
            )
        if self.mode is not LLMAssistMode.SHADOW:
            raise ValueError("only shadow mode is supported for human review summary")
        if self.succeeded:
            if self.fallback_reason is not None:
                raise ValueError(
                    "successful human review summary must not carry fallback_reason"
                )
            if self.summary is None:
                raise ValueError(
                    "successful human review summary requires a summary"
                )
        else:
            if self.fallback_reason is None:
                raise ValueError(
                    "failed human review summary requires fallback_reason"
                )
            if self.summary is not None:
                raise ValueError(
                    "failed human review summary must not carry a summary"
                )
        return self


def compute_prompt_hash(user_prompt_ko: str, context: LLMAssistContext) -> str:
    """Stable hash over prompt + catalog snapshot for shadow traceability."""

    canonical = json.dumps(
        {
            "schema_version": LLM_ASSIST_SCHEMA_VERSION,
            "user_prompt_ko": user_prompt_ko,
            "known_subjects": sorted(context.known_subjects),
            "known_categories": sorted(context.known_categories),
            "known_styles": sorted(context.known_styles),
            "catalog_snapshot_id": context.catalog_snapshot_id,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def human_review_summary_has_unsafe_approval_assertion(
    summary: HumanReviewSummary,
) -> bool:
    """Return whether a summary appears to assert approval or release.

    This is intentionally phrase-oriented. Review framing such as "법무 승인
    여부를 확인" is allowed, while direct clearance/pass/release assertions are
    rejected.
    """

    return human_review_text_has_unsafe_approval_assertion(
        (
            summary.evidence_summary_ko,
            *summary.checklist_ko,
            *summary.risk_notes_ko,
            *summary.suggested_human_questions_ko,
        )
    )


def human_review_text_has_unsafe_approval_assertion(
    values: Iterable[str],
) -> bool:
    """Return whether free text appears to assert review approval."""

    text = " ".join(values).casefold()
    return any(
        term.casefold() in text
        for term in _UNSAFE_HUMAN_REVIEW_ASSERTION_TERMS
    )
