"""Phase 12Z-A local LLM assist facade (shadow mode only).

``LLMAssistFacade`` is the single seam that owns local LLM calls for the
``newbie_request`` router. It is advisory: it records what a local/Ollama
model would propose next to the deterministic product decision and never
mutates route, gate, artifact, catalog, legal, visual, or release state.

Failure handling is explicit. A missing provider, timeout, empty response,
provider error, or schema-invalid response each degrade to a recorded
``LLMAssistShadowResult`` with a ``fallback_reason``. None of these are
treated as success, so silent fallback cannot happen.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterable
from typing import TYPE_CHECKING

from pydantic import ValidationError

from modules.llm.exceptions import LLMProviderError, LLMTimeoutError
from modules.llm.schemas import LLMMessage, LLMRequest
from modules.newbie_request.asset_catalog import load_decorative_asset_catalog
from modules.newbie_request.llm_schemas import (
    LLM_ASSIST_SCHEMA_VERSION,
    DraftSpecProposal,
    DraftSpecProposalSource,
    DraftSpecShadowSkipReason,
    FailureRepairAdvice,
    FailureRepairInput,
    FailureRepairShadowResult,
    FailureRepairStage,
    HumanReviewSummary,
    HumanReviewSummaryInput,
    HumanReviewSummaryShadowResult,
    LLMAdvisoryCandidateRoute,
    LLMAssistContext,
    LLMAssistFailureReason,
    LLMAssistMode,
    LLMAssistShadowResult,
    LLMDraftSpecShadowResult,
    LLMRequirementCandidate,
    LLMSearchExpansionCandidate,
    compute_prompt_hash,
    human_review_summary_has_unsafe_approval_assertion,
)
from modules.newbie_request.schemas import NewbieRoute

if TYPE_CHECKING:
    from modules.llm.base import BaseLLMProvider
    from modules.newbie_request.natural_language import NaturalLanguageRouteResult

__all__ = [
    "LLMAssistFacade",
    "draft_spec_shadow_skip_reason",
    "default_shadow_context",
    "failure_repair_advice_has_unsafe_circumvention",
    "human_review_summary_has_unsafe_approval_assertion",
    "should_run_draft_spec_shadow",
    "should_run_failure_repair_shadow",
    "should_run_clarifier_shadow",
]

_RAW_EXCERPT_LIMIT = 600
_DEFAULT_MODEL_NAME = "local-shadow-unconfigured"
_NONE_PROVIDER_NAME = "none"
_UNSAFE_CIRCUMVENTION_TERMS = (
    "상표 무시",
    "상표를 무시",
    "법무 검토 없이",
    "수동 검토 없이",
    "manual review 없이",
    "검토 없이 출시",
    "release_allowed=true",
    "printability gate 비활성",
    "visual gate 비활성",
    "게이트를 우회",
    "승인 없이",
)

_SYSTEM_PROMPT_KO = (
    "3D 프린팅 입문자 요청을 아래 JSON으로만 구조화하세요. "
    "출력은 참고용 shadow이며 실제 제작 경로나 생성 권한을 바꾸지 않습니다. "
    "반드시 JSON object 1개만 출력하세요. Markdown, 설명, 주석, 여러 JSON은 금지입니다.\n"
    "{\n"
    '  "category": string|null,\n'
    '  "object_type": string|null,\n'
    '  "subject_candidates": string[],\n'
    '  "style": string|null,\n'
    '  "required_features": string[],\n'
    '  "clarification_required": boolean,\n'
    '  "clarification_question": string|null,\n'
    '  "confidence": number(0..1),\n'
    '  "search_expansion": {\n'
    '    "subject_terms": string[],\n'
    '    "style_terms": string[],\n'
    '    "category_terms": string[]\n'
    "  }|null\n"
    "}"
)

_DRAFT_SPEC_SYSTEM_PROMPT_KO = (
    "입문자 3D 출력물 요청을 draft spec proposal JSON으로만 구조화하세요. "
    "이 출력은 참고용 shadow 제안이며 실제 초안 생성, CAD, STL, G-code, 법무 승인, "
    "출시 승인 권한이 없습니다. 반드시 JSON object 1개만 출력하세요. "
    "Markdown, 설명, 주석, 여러 JSON은 금지입니다.\n"
    "{\n"
    '  "subject": string,\n'
    '  "category": string,\n'
    '  "style": string|null,\n'
    '  "required_features": string[],\n'
    '  "size_hint_mm": {"width": number, "depth": number, "height": number},\n'
    '  "visual_intent_ko": string,\n'
    '  "risk_flags": string[],\n'
    '  "confidence": number(0..1),\n'
    '  "source": "local_llm_shadow"\n'
    "}\n"
    "license, legal_review_status, release_allowed, provenance 필드는 절대 넣지 마세요."
)

_FAILURE_REPAIR_SYSTEM_PROMPT_KO = (
    "3D 출력물 생성 실패 원인을 사용자에게 설명하고 다음 행동을 제안하세요. "
    "이 출력은 참고용 shadow advice이며 실제 retry, route 변경, 생성, 승인 권한이 없습니다. "
    "상표/IP, 법무 검토, 수동 검토 차단을 우회하라고 조언하면 안 됩니다. "
    "printability, visual, manual review gate를 비활성화하라고 조언하면 안 됩니다. "
    "반드시 JSON object 1개만 출력하세요. Markdown, 설명, 주석은 금지입니다.\n"
    "{\n"
    '  "failure_stage": '
    '"semantic_validation|printability|visual_quality|route_selection|manual_review",\n'
    '  "deterministic_reason": string,\n'
    '  "user_facing_summary_ko": string,\n'
    '  "suggested_next_action": '
    '"add_missing_features|provide_dimensions|choose_supported_subject|requires_manual_review",\n'
    '  "missing_information": string[],\n'
    '  "retry_prompt_hint_ko": string|null,\n'
    '  "risk_flags": string[],\n'
    '  "used_for_decision": false\n'
    "}"
)

_HUMAN_REVIEW_SUMMARY_SYSTEM_PROMPT_KO = (
    "3D 출력물의 시각/법무/출력성/등록 검토 증거를 사람이 읽기 쉽게 요약하세요. "
    "이 출력은 참고용 shadow summary이며 실제 승인, 통과, 출시, 등록, 법무 clearance "
    "권한이 없습니다. 통과/승인/출시 가능하다고 단정하지 말고, 증거 요약, 체크리스트, "
    "위험 메모, 사람이 확인할 질문만 작성하세요. 반드시 JSON object 1개만 출력하세요. "
    "Markdown, 설명, 주석은 금지입니다.\n"
    "{\n"
    '  "review_context": string,\n'
    '  "evidence_summary_ko": string,\n'
    '  "checklist_ko": string[],\n'
    '  "risk_notes_ko": string[],\n'
    '  "suggested_human_questions_ko": string[],\n'
    '  "used_for_decision": false\n'
    "}"
)


class LLMAssistFacade:
    """Non-authoritative local LLM helper for shadow comparison."""

    def __init__(
        self,
        *,
        provider: BaseLLMProvider | None = None,
        model_name: str | None = None,
        max_candidates: int = 3,
        max_tokens: int = 512,
    ) -> None:
        if max_candidates <= 0:
            raise ValueError("max_candidates must be positive")
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        self._provider = provider
        self._model_name = model_name or _DEFAULT_MODEL_NAME
        self._max_candidates = max_candidates
        self._max_tokens = max_tokens

    @property
    def provider_configured(self) -> bool:
        return self._provider is not None

    async def clarify_requirement_shadow(
        self,
        user_prompt_ko: str,
        deterministic_result: NaturalLanguageRouteResult,
        context: LLMAssistContext,
    ) -> LLMAssistShadowResult:
        """Record an advisory shadow candidate next to the product decision.

        Preconditions:
            ``deterministic_result`` is the authoritative product route. It is
            never mutated here.
        Postconditions:
            Returns a schema-valid shadow result. On any provider or schema
            failure the result carries ``succeeded=False`` and a
            ``fallback_reason``.
        """

        prompt_hash = compute_prompt_hash(user_prompt_ko, context)
        deterministic_route = deterministic_result.selected_route.value

        if self._provider is None:
            return self._failure(
                prompt_hash=prompt_hash,
                deterministic_route=deterministic_route,
                provider_name=_NONE_PROVIDER_NAME,
                model_name=self._model_name,
                reason=LLMAssistFailureReason.PROVIDER_NOT_CONFIGURED,
            )

        request = LLMRequest(
            messages=(
                LLMMessage(role="system", content=_SYSTEM_PROMPT_KO),
                LLMMessage(
                    role="user",
                    content=_build_user_prompt(
                        user_prompt_ko,
                        context,
                        max_candidates=self._max_candidates,
                    ),
                ),
            ),
            model=self._model_name,
            temperature=0.0,
            max_tokens=self._max_tokens,
        )

        try:
            response = await self._provider.complete(request)
        except LLMTimeoutError:
            return self._failure(
                prompt_hash=prompt_hash,
                deterministic_route=deterministic_route,
                provider_name=self._provider.provider_name,
                model_name=self._model_name,
                reason=LLMAssistFailureReason.TIMEOUT,
            )
        except LLMProviderError:
            return self._failure(
                prompt_hash=prompt_hash,
                deterministic_route=deterministic_route,
                provider_name=self._provider.provider_name,
                model_name=self._model_name,
                reason=LLMAssistFailureReason.PROVIDER_ERROR,
            )
        except (RuntimeError, OSError, ValueError):
            # Provider adapters may wrap transport/runtime failures in
            # non-LLM exceptions. Shadow mode records those at the provider
            # boundary, but still never promotes them into product decisions.
            return self._failure(
                prompt_hash=prompt_hash,
                deterministic_route=deterministic_route,
                provider_name=self._provider.provider_name,
                model_name=self._model_name,
                reason=LLMAssistFailureReason.PROVIDER_ERROR,
            )

        content = response.content.strip()
        if not content:
            return self._failure(
                prompt_hash=prompt_hash,
                deterministic_route=deterministic_route,
                provider_name=response.provider,
                model_name=response.model,
                reason=LLMAssistFailureReason.EMPTY_RESPONSE,
            )

        try:
            candidate, expansion = _parse_candidate(
                content,
                max_candidates=self._max_candidates,
            )
        except (json.JSONDecodeError, ValidationError, ValueError):
            return self._failure(
                prompt_hash=prompt_hash,
                deterministic_route=deterministic_route,
                provider_name=response.provider,
                model_name=response.model,
                reason=LLMAssistFailureReason.SCHEMA_INVALID,
                raw_response_excerpt=_excerpt(content),
            )

        return LLMAssistShadowResult(
            mode=LLMAssistMode.SHADOW,
            succeeded=True,
            used_for_decision=False,
            model_name=response.model,
            provider_name=response.provider,
            prompt_hash=prompt_hash,
            schema_version=LLM_ASSIST_SCHEMA_VERSION,
            deterministic_route=deterministic_route,
            confidence=candidate.confidence,
            fallback_reason=None,
            candidate_route=_advisory_candidate_route(candidate),
            candidate_subjects=candidate.subject_candidates,
            candidate_style=candidate.style,
            clarification_question=candidate.clarification_question,
            requirement_candidate=candidate,
            search_expansion=expansion,
            raw_response_excerpt=_excerpt(content),
        )

    async def propose_draft_spec_shadow(
        self,
        user_prompt_ko: str,
        deterministic_result: NaturalLanguageRouteResult,
        context: LLMAssistContext,
    ) -> LLMDraftSpecShadowResult:
        """Record an advisory draft spec proposal next to the product decision.

        This method never creates ``DraftAssetEntry`` and never changes route,
        generation, legal, visual, release, CAD, STL, or Orca state.
        """

        prompt_hash = compute_prompt_hash(user_prompt_ko, context)
        deterministic_route = deterministic_result.selected_route.value

        if self._provider is None:
            return self._draft_spec_failure(
                prompt_hash=prompt_hash,
                deterministic_route=deterministic_route,
                provider_name=_NONE_PROVIDER_NAME,
                model_name=self._model_name,
                reason=LLMAssistFailureReason.PROVIDER_NOT_CONFIGURED,
            )

        request = LLMRequest(
            messages=(
                LLMMessage(role="system", content=_DRAFT_SPEC_SYSTEM_PROMPT_KO),
                LLMMessage(
                    role="user",
                    content=_build_draft_spec_user_prompt(user_prompt_ko, context),
                ),
            ),
            model=self._model_name,
            temperature=0.0,
            max_tokens=self._max_tokens,
        )

        try:
            response = await self._provider.complete(request)
        except LLMTimeoutError:
            return self._draft_spec_failure(
                prompt_hash=prompt_hash,
                deterministic_route=deterministic_route,
                provider_name=self._provider.provider_name,
                model_name=self._model_name,
                reason=LLMAssistFailureReason.TIMEOUT,
            )
        except LLMProviderError:
            return self._draft_spec_failure(
                prompt_hash=prompt_hash,
                deterministic_route=deterministic_route,
                provider_name=self._provider.provider_name,
                model_name=self._model_name,
                reason=LLMAssistFailureReason.PROVIDER_ERROR,
            )
        except (RuntimeError, OSError, ValueError):
            return self._draft_spec_failure(
                prompt_hash=prompt_hash,
                deterministic_route=deterministic_route,
                provider_name=self._provider.provider_name,
                model_name=self._model_name,
                reason=LLMAssistFailureReason.PROVIDER_ERROR,
            )

        content = response.content.strip()
        if not content:
            return self._draft_spec_failure(
                prompt_hash=prompt_hash,
                deterministic_route=deterministic_route,
                provider_name=response.provider,
                model_name=response.model,
                reason=LLMAssistFailureReason.EMPTY_RESPONSE,
            )

        try:
            proposal = _parse_draft_spec_proposal(content)
        except (json.JSONDecodeError, ValidationError, ValueError):
            return self._draft_spec_failure(
                prompt_hash=prompt_hash,
                deterministic_route=deterministic_route,
                provider_name=response.provider,
                model_name=response.model,
                reason=LLMAssistFailureReason.SCHEMA_INVALID,
                raw_response_excerpt=_excerpt(content),
            )

        return LLMDraftSpecShadowResult(
            mode=LLMAssistMode.SHADOW,
            succeeded=True,
            used_for_decision=False,
            model_name=response.model,
            provider_name=response.provider,
            prompt_hash=prompt_hash,
            schema_version=LLM_ASSIST_SCHEMA_VERSION,
            deterministic_route=deterministic_route,
            confidence=proposal.confidence,
            fallback_reason=None,
            proposal=proposal,
            raw_response_excerpt=_excerpt(content),
        )

    async def propose_failure_repair_shadow(
        self,
        user_prompt_ko: str,
        deterministic_result: NaturalLanguageRouteResult,
        failure_input: FailureRepairInput,
        context: LLMAssistContext,
    ) -> FailureRepairShadowResult:
        """Record advisory failure-repair advice next to product behavior.

        This method never retries, never calls self-healer, and never changes
        route, generation, draft, legal, visual, release, CAD, STL, or Orca
        state.
        """

        prompt_hash = compute_prompt_hash(user_prompt_ko, context)
        deterministic_route = deterministic_result.selected_route.value

        if self._provider is None:
            return self._failure_repair_failure(
                prompt_hash=prompt_hash,
                deterministic_route=deterministic_route,
                provider_name=_NONE_PROVIDER_NAME,
                model_name=self._model_name,
                reason=LLMAssistFailureReason.PROVIDER_NOT_CONFIGURED,
                failure_input=failure_input,
            )

        request = LLMRequest(
            messages=(
                LLMMessage(role="system", content=_FAILURE_REPAIR_SYSTEM_PROMPT_KO),
                LLMMessage(
                    role="user",
                    content=_build_failure_repair_user_prompt(
                        user_prompt_ko,
                        deterministic_result,
                        failure_input,
                        context,
                    ),
                ),
            ),
            model=self._model_name,
            temperature=0.0,
            max_tokens=self._max_tokens,
        )

        try:
            response = await self._provider.complete(request)
        except LLMTimeoutError:
            return self._failure_repair_failure(
                prompt_hash=prompt_hash,
                deterministic_route=deterministic_route,
                provider_name=self._provider.provider_name,
                model_name=self._model_name,
                reason=LLMAssistFailureReason.TIMEOUT,
                failure_input=failure_input,
            )
        except LLMProviderError:
            return self._failure_repair_failure(
                prompt_hash=prompt_hash,
                deterministic_route=deterministic_route,
                provider_name=self._provider.provider_name,
                model_name=self._model_name,
                reason=LLMAssistFailureReason.PROVIDER_ERROR,
                failure_input=failure_input,
            )
        except (RuntimeError, OSError, ValueError):
            return self._failure_repair_failure(
                prompt_hash=prompt_hash,
                deterministic_route=deterministic_route,
                provider_name=self._provider.provider_name,
                model_name=self._model_name,
                reason=LLMAssistFailureReason.PROVIDER_ERROR,
                failure_input=failure_input,
            )

        content = response.content.strip()
        if not content:
            return self._failure_repair_failure(
                prompt_hash=prompt_hash,
                deterministic_route=deterministic_route,
                provider_name=response.provider,
                model_name=response.model,
                reason=LLMAssistFailureReason.EMPTY_RESPONSE,
                failure_input=failure_input,
            )

        try:
            advice = _parse_failure_repair_advice(content)
        except (json.JSONDecodeError, ValidationError, ValueError):
            return self._failure_repair_failure(
                prompt_hash=prompt_hash,
                deterministic_route=deterministic_route,
                provider_name=response.provider,
                model_name=response.model,
                reason=LLMAssistFailureReason.SCHEMA_INVALID,
                failure_input=failure_input,
                raw_response_excerpt=_excerpt(content),
            )

        return FailureRepairShadowResult(
            mode=LLMAssistMode.SHADOW,
            succeeded=True,
            used_for_decision=False,
            model_name=response.model,
            provider_name=response.provider,
            prompt_hash=prompt_hash,
            schema_version=LLM_ASSIST_SCHEMA_VERSION,
            deterministic_route=deterministic_route,
            confidence=None,
            fallback_reason=None,
            failure_input=failure_input,
            advice=advice,
            raw_response_excerpt=_excerpt(content),
        )

    async def summarize_human_review_shadow(
        self,
        user_prompt_ko: str,
        deterministic_result: NaturalLanguageRouteResult,
        review_input: HumanReviewSummaryInput,
        context: LLMAssistContext,
    ) -> HumanReviewSummaryShadowResult:
        """Record advisory review-summary text next to product behavior.

        This method intentionally has no call-gate: manual/legal review facts
        are the subject being summarized. The safety boundary is content-only:
        the summary must not assert approval, pass, release, or legal clearance.
        """

        prompt_hash = compute_prompt_hash(user_prompt_ko, context)
        deterministic_route = deterministic_result.selected_route.value

        if self._provider is None:
            return self._human_review_summary_failure(
                prompt_hash=prompt_hash,
                deterministic_route=deterministic_route,
                provider_name=_NONE_PROVIDER_NAME,
                model_name=self._model_name,
                reason=LLMAssistFailureReason.PROVIDER_NOT_CONFIGURED,
                review_input=review_input,
            )

        request = LLMRequest(
            messages=(
                LLMMessage(
                    role="system",
                    content=_HUMAN_REVIEW_SUMMARY_SYSTEM_PROMPT_KO,
                ),
                LLMMessage(
                    role="user",
                    content=_build_human_review_summary_user_prompt(
                        user_prompt_ko,
                        deterministic_result,
                        review_input,
                        context,
                    ),
                ),
            ),
            model=self._model_name,
            temperature=0.0,
            max_tokens=self._max_tokens,
        )

        try:
            response = await self._provider.complete(request)
        except LLMTimeoutError:
            return self._human_review_summary_failure(
                prompt_hash=prompt_hash,
                deterministic_route=deterministic_route,
                provider_name=self._provider.provider_name,
                model_name=self._model_name,
                reason=LLMAssistFailureReason.TIMEOUT,
                review_input=review_input,
            )
        except LLMProviderError:
            return self._human_review_summary_failure(
                prompt_hash=prompt_hash,
                deterministic_route=deterministic_route,
                provider_name=self._provider.provider_name,
                model_name=self._model_name,
                reason=LLMAssistFailureReason.PROVIDER_ERROR,
                review_input=review_input,
            )
        except (RuntimeError, OSError, ValueError):
            return self._human_review_summary_failure(
                prompt_hash=prompt_hash,
                deterministic_route=deterministic_route,
                provider_name=self._provider.provider_name,
                model_name=self._model_name,
                reason=LLMAssistFailureReason.PROVIDER_ERROR,
                review_input=review_input,
            )

        content = response.content.strip()
        if not content:
            return self._human_review_summary_failure(
                prompt_hash=prompt_hash,
                deterministic_route=deterministic_route,
                provider_name=response.provider,
                model_name=response.model,
                reason=LLMAssistFailureReason.EMPTY_RESPONSE,
                review_input=review_input,
            )

        try:
            summary = _parse_human_review_summary(content)
        except (json.JSONDecodeError, ValidationError, ValueError):
            return self._human_review_summary_failure(
                prompt_hash=prompt_hash,
                deterministic_route=deterministic_route,
                provider_name=response.provider,
                model_name=response.model,
                reason=LLMAssistFailureReason.SCHEMA_INVALID,
                review_input=review_input,
                raw_response_excerpt=_excerpt(content),
            )

        return HumanReviewSummaryShadowResult(
            mode=LLMAssistMode.SHADOW,
            succeeded=True,
            used_for_decision=False,
            model_name=response.model,
            provider_name=response.provider,
            prompt_hash=prompt_hash,
            schema_version=LLM_ASSIST_SCHEMA_VERSION,
            deterministic_route=deterministic_route,
            confidence=None,
            fallback_reason=None,
            review_input=review_input,
            summary=summary,
            raw_response_excerpt=_excerpt(content),
        )

    def _failure(
        self,
        *,
        prompt_hash: str,
        deterministic_route: str,
        provider_name: str,
        model_name: str,
        reason: LLMAssistFailureReason,
        raw_response_excerpt: str | None = None,
    ) -> LLMAssistShadowResult:
        return LLMAssistShadowResult(
            mode=LLMAssistMode.SHADOW,
            succeeded=False,
            used_for_decision=False,
            model_name=model_name,
            provider_name=provider_name,
            prompt_hash=prompt_hash,
            schema_version=LLM_ASSIST_SCHEMA_VERSION,
            deterministic_route=deterministic_route,
            confidence=None,
            fallback_reason=reason,
            requirement_candidate=None,
            search_expansion=None,
            raw_response_excerpt=raw_response_excerpt,
        )

    def _draft_spec_failure(
        self,
        *,
        prompt_hash: str,
        deterministic_route: str,
        provider_name: str,
        model_name: str,
        reason: LLMAssistFailureReason,
        raw_response_excerpt: str | None = None,
    ) -> LLMDraftSpecShadowResult:
        return LLMDraftSpecShadowResult(
            mode=LLMAssistMode.SHADOW,
            succeeded=False,
            used_for_decision=False,
            model_name=model_name,
            provider_name=provider_name,
            prompt_hash=prompt_hash,
            schema_version=LLM_ASSIST_SCHEMA_VERSION,
            deterministic_route=deterministic_route,
            confidence=None,
            fallback_reason=reason,
            proposal=None,
            raw_response_excerpt=raw_response_excerpt,
        )

    def _failure_repair_failure(
        self,
        *,
        prompt_hash: str,
        deterministic_route: str,
        provider_name: str,
        model_name: str,
        reason: LLMAssistFailureReason,
        failure_input: FailureRepairInput,
        raw_response_excerpt: str | None = None,
    ) -> FailureRepairShadowResult:
        return FailureRepairShadowResult(
            mode=LLMAssistMode.SHADOW,
            succeeded=False,
            used_for_decision=False,
            model_name=model_name,
            provider_name=provider_name,
            prompt_hash=prompt_hash,
            schema_version=LLM_ASSIST_SCHEMA_VERSION,
            deterministic_route=deterministic_route,
            confidence=None,
            fallback_reason=reason,
            failure_input=failure_input,
            advice=None,
            raw_response_excerpt=raw_response_excerpt,
        )

    def _human_review_summary_failure(
        self,
        *,
        prompt_hash: str,
        deterministic_route: str,
        provider_name: str,
        model_name: str,
        reason: LLMAssistFailureReason,
        review_input: HumanReviewSummaryInput,
        raw_response_excerpt: str | None = None,
    ) -> HumanReviewSummaryShadowResult:
        return HumanReviewSummaryShadowResult(
            mode=LLMAssistMode.SHADOW,
            succeeded=False,
            used_for_decision=False,
            model_name=model_name,
            provider_name=provider_name,
            prompt_hash=prompt_hash,
            schema_version=LLM_ASSIST_SCHEMA_VERSION,
            deterministic_route=deterministic_route,
            confidence=None,
            fallback_reason=reason,
            review_input=review_input,
            summary=None,
            raw_response_excerpt=raw_response_excerpt,
        )


def should_run_clarifier_shadow(
    route_result: NaturalLanguageRouteResult,
) -> bool:
    """Return whether advisory clarifier shadow may call the local LLM.

    The allowlist is intentionally narrower than all natural-language routes:
    only deterministic ASK_USER clarification cases are measured. Trademark/IP
    manual-review boundaries are excluded so local LLMs cannot soften or
    reinterpret rights-sensitive prompts.
    """

    from modules.newbie_request.natural_language import RequirementExtractionReason

    allowed_reasons = {
        RequirementExtractionReason.UNKNOWN_DECORATIVE_SUBJECT,
        RequirementExtractionReason.AMBIGUOUS_DECORATIVE_SUBJECT,
        RequirementExtractionReason.MISSING_MECHANICAL_DIMENSIONS,
        RequirementExtractionReason.UNSUPPORTED_OR_AMBIGUOUS_ROUTE,
    }
    return (
        route_result.selected_route is NewbieRoute.ASK_USER
        and route_result.clarification_required
        and route_result.requirement.clarification_reason in allowed_reasons
    )


def should_run_draft_spec_shadow(
    route_result: NaturalLanguageRouteResult,
) -> bool:
    """Return whether product wiring may call draft-spec shadow.

    This is a deterministic precondition for future D2 wiring. It is not used
    by measurement runners, and it must never mutate route, generation, draft,
    legal, visual, release, CAD, STL, or Orca state.
    """

    return draft_spec_shadow_skip_reason(route_result) is None


def draft_spec_shadow_skip_reason(
    route_result: NaturalLanguageRouteResult,
) -> DraftSpecShadowSkipReason | None:
    """Return why draft-spec shadow must be skipped, or ``None`` when allowed."""

    from modules.newbie_request.asset_intake import AssetIntakeStatus
    from modules.newbie_request.natural_language import (
        RequirementExtractionReason,
        RouteIntegrationReason,
    )
    from modules.newbie_request.subject_aliases import (
        load_trademark_blocked_subjects,
    )

    requirement = route_result.requirement
    intake = route_result.intake_decision
    intake_status = intake.status if intake is not None else None

    if intake_status == AssetIntakeStatus.RUNTIME_CATALOG_MATCH.value:
        return DraftSpecShadowSkipReason.RUNTIME_ASSET_ALREADY_AVAILABLE
    if intake_status in {
        AssetIntakeStatus.DRAFT_QUEUE_MATCH.value,
        AssetIntakeStatus.DUPLICATE_OR_SIMILAR_CANDIDATE.value,
    }:
        return DraftSpecShadowSkipReason.DRAFT_OR_REFERENCE_REUSE_AVAILABLE

    if (
        route_result.reason is RouteIntegrationReason.MANUAL_REVIEW_REQUIRED
        or requirement.clarification_reason
        is RequirementExtractionReason.TRADEMARK_BLOCKED_SUBJECT
        or load_trademark_blocked_subjects().match(requirement.user_prompt_ko)
        is not None
    ):
        return DraftSpecShadowSkipReason.TRADEMARK_OR_MANUAL_REVIEW_BOUNDARY

    if route_result.clarification_required or route_result.selected_route is NewbieRoute.ASK_USER:
        if (
            requirement.clarification_reason
            is RequirementExtractionReason.UNKNOWN_DECORATIVE_SUBJECT
        ):
            return DraftSpecShadowSkipReason.UNKNOWN_SUBJECT
        return DraftSpecShadowSkipReason.ASK_USER_REQUIRES_CLARIFICATION

    if (
        route_result.selected_route is not NewbieRoute.DRAFT_ASSET
        or route_result.reason is not RouteIntegrationReason.DRAFT_AUTHORING_REQUIRED
        or intake is None
        or intake_status != AssetIntakeStatus.NEW_DRAFT_ALLOWED.value
        or not intake.new_draft_allowed
    ):
        return DraftSpecShadowSkipReason.ROUTE_NOT_NEW_DRAFT_ALLOWED

    if (
        requirement.category != "decorative_keyring"
        or requirement.subject is None
        or not _is_supported_draft_spec_subject(requirement.subject)
    ):
        return DraftSpecShadowSkipReason.UNKNOWN_SUBJECT

    return None


def should_run_failure_repair_shadow(
    route_result: NaturalLanguageRouteResult,
    failure_input: FailureRepairInput,
) -> bool:
    """Return whether product wiring may call failure-repair shadow."""

    from modules.newbie_request.natural_language import (
        RequirementExtractionReason,
        RouteIntegrationReason,
    )
    from modules.newbie_request.subject_aliases import (
        load_trademark_blocked_subjects,
    )

    if failure_input.legal_review_required or failure_input.manual_review_required:
        return False
    if failure_input.failure_stage is FailureRepairStage.MANUAL_REVIEW:
        return False
    if route_result.reason is RouteIntegrationReason.MANUAL_REVIEW_REQUIRED:
        return False
    if (
        route_result.requirement.clarification_reason
        is RequirementExtractionReason.TRADEMARK_BLOCKED_SUBJECT
    ):
        return False
    if (
        load_trademark_blocked_subjects().match(
            route_result.requirement.user_prompt_ko
        )
        is not None
    ):
        return False
    return failure_input.failure_stage in {
        FailureRepairStage.SEMANTIC_VALIDATION,
        FailureRepairStage.PRINTABILITY,
        FailureRepairStage.VISUAL_QUALITY,
        FailureRepairStage.ROUTE_SELECTION,
    }


def failure_repair_advice_has_unsafe_circumvention(
    advice: FailureRepairAdvice,
) -> bool:
    """Return whether free-text advice appears to bypass safety gates."""

    text = " ".join(
        value
        for value in (
            advice.user_facing_summary_ko,
            advice.retry_prompt_hint_ko or "",
            " ".join(advice.risk_flags),
        )
        if value
    ).casefold()
    return any(term.casefold() in text for term in _UNSAFE_CIRCUMVENTION_TERMS)


def default_shadow_context() -> LLMAssistContext:
    """Repo-local catalog snapshot used by the deterministic newbie router."""

    try:
        catalog = load_decorative_asset_catalog()
    except (OSError, json.JSONDecodeError, ValidationError, ValueError):
        return LLMAssistContext(
            known_subjects=(),
            known_categories=(),
            known_styles=(),
            catalog_snapshot_id="decorative_asset_catalog:unavailable",
        )

    return LLMAssistContext(
        known_subjects=_sorted_unique(asset.subject for asset in catalog.assets),
        known_categories=_sorted_unique(asset.category for asset in catalog.assets),
        known_styles=_sorted_unique(asset.style for asset in catalog.assets),
        catalog_snapshot_id=_catalog_snapshot_id(
            asset.asset_id for asset in catalog.assets
        ),
    )


def _advisory_candidate_route(candidate: LLMRequirementCandidate) -> str:
    """Advisory only mapping; never used to authorize a real route."""

    if candidate.clarification_required:
        return NewbieRoute.ASK_USER.value
    return LLMAdvisoryCandidateRoute.CANDIDATE_SUBJECT_PROPOSED.value


def _build_user_prompt(
    user_prompt_ko: str,
    context: LLMAssistContext,
    *,
    max_candidates: int,
) -> str:
    known_subjects = ", ".join(context.known_subjects) or "(none)"
    known_categories = ", ".join(context.known_categories) or "(none)"
    known_styles = ", ".join(context.known_styles) or "(none)"
    return (
        f"사용자 요청: {user_prompt_ko}\n"
        f"known_subjects: {known_subjects}\n"
        f"known_categories: {known_categories}\n"
        f"known_styles: {known_styles}\n"
        f"subject_candidates는 최대 {max_candidates}개입니다.\n"
        "요청이 곰, 토끼, 고양이처럼 구체적 subject를 말하면 "
        "clarification_required=false 로 두세요.\n"
        "동물 키링처럼 subject가 모호하거나 catalog 밖/OOD이면 "
        "clarification_required=true 와 한국어 clarification_question 을 채우세요.\n"
        "확신이 낮으면 subject를 추측해 확정하지 말고 clarification_required=true 로 두세요."
    )


def _build_draft_spec_user_prompt(
    user_prompt_ko: str,
    context: LLMAssistContext,
) -> str:
    known_subjects = ", ".join(context.known_subjects) or "(none)"
    known_categories = ", ".join(context.known_categories) or "(none)"
    known_styles = ", ".join(context.known_styles) or "(none)"
    return (
        f"사용자 요청: {user_prompt_ko}\n"
        f"known_subjects: {known_subjects}\n"
        f"known_categories: {known_categories}\n"
        f"known_styles: {known_styles}\n"
        "draft spec proposal은 측정용입니다. 초안 생성/승인 권한이 없습니다.\n"
        'source는 반드시 "local_llm_shadow" 입니다.\n'
        "상표/IP, 확실하지 않은 subject, catalog 밖 subject는 risk_flags에 명시하세요.\n"
        "size_hint_mm는 mm 단위이며 width/depth/height 모두 양수여야 합니다."
    )


def _build_failure_repair_user_prompt(
    user_prompt_ko: str,
    route_result: NaturalLanguageRouteResult,
    failure_input: FailureRepairInput,
    context: LLMAssistContext,
) -> str:
    known_subjects = ", ".join(context.known_subjects) or "(none)"
    payload = failure_input.model_dump(mode="json")
    return (
        f"사용자 요청: {user_prompt_ko}\n"
        f"deterministic_route: {route_result.selected_route.value}\n"
        f"generation_allowed: {route_result.generation_allowed}\n"
        f"known_subjects: {known_subjects}\n"
        f"failure_input_json: {json.dumps(payload, ensure_ascii=False, sort_keys=True)}\n"
        "사용자에게 내부 구현명은 노출하지 말고, 필요한 다음 행동만 설명하세요.\n"
        "상표/IP, 법무, 수동 검토, printability, visual quality gate를 우회하지 마세요."
    )


def _build_human_review_summary_user_prompt(
    user_prompt_ko: str,
    route_result: NaturalLanguageRouteResult,
    review_input: HumanReviewSummaryInput,
    context: LLMAssistContext,
) -> str:
    known_subjects = ", ".join(context.known_subjects) or "(none)"
    payload = review_input.model_dump(mode="json")
    return (
        f"사용자 요청: {user_prompt_ko}\n"
        f"deterministic_route: {route_result.selected_route.value}\n"
        f"generation_allowed: {route_result.generation_allowed}\n"
        f"known_subjects: {known_subjects}\n"
        f"review_input_json: {json.dumps(payload, ensure_ascii=False, sort_keys=True)}\n"
        "요약은 사람이 검토할 증거와 질문을 준비하기 위한 것입니다.\n"
        "승인됨, 통과, 출시 가능, 법무 clearance 같은 결론을 단정하지 마세요."
    )


def _parse_candidate(
    content: str,
    *,
    max_candidates: int,
) -> tuple[LLMRequirementCandidate, LLMSearchExpansionCandidate | None]:
    payload = json.loads(_extract_json_object(content))
    if not isinstance(payload, dict):
        raise ValueError("LLM response must be a JSON object")

    raw_expansion = payload.pop("search_expansion", None)
    expansion: LLMSearchExpansionCandidate | None = None
    if raw_expansion is not None:
        if not isinstance(raw_expansion, dict):
            raise ValueError("search_expansion must be a JSON object or null")
        expansion = LLMSearchExpansionCandidate(**raw_expansion)

    candidate = LLMRequirementCandidate(**payload)
    if len(candidate.subject_candidates) > max_candidates:
        raise ValueError("subject_candidates exceeds max_candidates")
    return candidate, expansion


def _parse_draft_spec_proposal(content: str) -> DraftSpecProposal:
    payload = json.loads(_extract_json_object(content))
    if not isinstance(payload, dict):
        raise ValueError("LLM draft spec response must be a JSON object")
    proposal = DraftSpecProposal(**payload)
    if proposal.source is not DraftSpecProposalSource.LOCAL_LLM_SHADOW:
        raise ValueError("live draft spec proposal must use source=local_llm_shadow")
    return proposal


def _parse_failure_repair_advice(content: str) -> FailureRepairAdvice:
    payload = json.loads(_extract_json_object(content))
    if not isinstance(payload, dict):
        raise ValueError("LLM failure repair response must be a JSON object")
    return FailureRepairAdvice(**payload)


def _parse_human_review_summary(content: str) -> HumanReviewSummary:
    payload = json.loads(_extract_json_object(content))
    if not isinstance(payload, dict):
        raise ValueError("LLM human review summary response must be a JSON object")
    return HumanReviewSummary(**payload)


def _strip_code_fence(content: str) -> str:
    stripped = content.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _extract_json_object(content: str) -> str:
    stripped = _strip_code_fence(content)
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        return stripped
    candidate = stripped[start : end + 1]
    # Bounded repair only: accept a single valid JSON object substring. Do not
    # infer missing keys, merge objects, fix typos, or complete truncated JSON.
    parsed = json.loads(candidate)
    if not isinstance(parsed, dict):
        raise ValueError("LLM response JSON substring must be an object")
    return candidate


def _excerpt(content: str) -> str:
    sanitized = "".join(
        char
        if char in {"\n", "\r", "\t"} or unicodedata.category(char) != "Cc"
        else " "
        for char in content
    )
    if len(sanitized) <= _RAW_EXCERPT_LIMIT:
        return sanitized
    return sanitized[:_RAW_EXCERPT_LIMIT]


def _sorted_unique(values: Iterable[str | None]) -> tuple[str, ...]:
    return tuple(sorted({value for value in values if value}))


def _is_supported_draft_spec_subject(subject: str) -> bool:
    from modules.newbie_request.subject_aliases import load_subject_alias_table

    return any(
        row.subject == subject and row.category == "decorative_keyring"
        for row in load_subject_alias_table().aliases
    )


def _catalog_snapshot_id(asset_ids: Iterable[str]) -> str:
    joined = ",".join(sorted(asset_ids))
    return f"decorative_asset_catalog:{joined}" if joined else "decorative_asset_catalog:empty"
