from __future__ import annotations

from modules.orchestrator_decision.base import BaseDecisionEngine
from modules.orchestrator_decision.schemas import (
    DecisionAction,
    DecisionReason,
    DecisionRequest,
    DecisionResult,
    ProviderPolicy,
)
from modules.semantic_validator.schemas import SemanticValidationReport

_LOCAL_PROVIDERS = {"local", "ollama", "qwen", "ollama_qwen", "qwen_local"}


class RuleBasedDecisionEngine(BaseDecisionEngine):
    """Deterministic Phase 11C-1 decision matrix implementation."""

    @property
    def engine_name(self) -> str:
        return "rule_based"

    def decide(self, request: DecisionRequest) -> DecisionResult:
        if _budget_exceeded(request):
            return _fail(
                request,
                reason=DecisionReason.BUDGET_EXCEEDED,
                detail="Cost or timeout budget was exceeded.",
            )

        if _quota_exceeded(request):
            return _fail(
                request,
                reason=DecisionReason.QUOTA_EXCEEDED,
                detail="Provider quota is exhausted.",
            )

        if request.product_mode and _is_mock_provider(request.provider):
            return _fail(
                request,
                reason=DecisionReason.MOCK_PROVIDER_IN_PRODUCT_PATH,
                detail="Mock provider is not allowed in product mode.",
            )

        if not request.approval_snapshot_matches:
            return _fail(
                request,
                reason=DecisionReason.APPROVAL_SNAPSHOT_MISMATCH,
                detail="Approved subtask snapshot does not match execution input.",
            )

        if _requires_clarification(request):
            return DecisionResult(
                action=DecisionAction.ASK_USER_CLARIFICATION,
                reason=DecisionReason.MISSING_REQUIRED_DIMENSION,
                detail="Required dimensions or clarifying answers are missing.",
                should_continue=False,
                questions=_clarifying_questions(request),
                trace_id=request.trace_id,
                metadata=_base_metadata(request),
            )

        if not request.provider_available:
            return _provider_unavailable_decision(request)

        if request.semantic_report is None:
            if _should_fallback_for_high_complexity(request):
                return _fallback_to_provider(request)
            if _is_provider_fallback_candidate(request):
                blocked = _blocked_provider_fallback(request)
                if blocked is not None:
                    return blocked
            return _fail(
                request,
                reason=DecisionReason.SEMANTIC_REPORT_MISSING,
                detail="Semantic report is required before slicing.",
            )

        return _semantic_decision(request, request.semantic_report)


def _semantic_decision(
    request: DecisionRequest,
    report: SemanticValidationReport,
) -> DecisionResult:
    if report.passed:
        return DecisionResult(
            action=DecisionAction.PROCEED_TO_SLICE,
            reason=DecisionReason.SEMANTIC_PASSED,
            detail="Semantic validation passed; slicing may proceed.",
            should_continue=True,
            trace_id=request.trace_id or report.trace_id,
            metadata=_semantic_metadata(request, report),
        )

    if _attempts_remain(request):
        return DecisionResult(
            action=DecisionAction.RETRY_WITH_SELF_HEALER,
            reason=DecisionReason.SEMANTIC_MISSING_FEATURES,
            detail="Semantic validation failed and retry budget remains.",
            should_continue=True,
            next_attempt=request.attempt_index + 1,
            trace_id=request.trace_id or report.trace_id,
            metadata=_semantic_metadata(request, report),
        )

    if _should_fallback_to_template(request):
        return DecisionResult(
            action=DecisionAction.FALLBACK_TO_TEMPLATE,
            reason=DecisionReason.SEMANTIC_RETRY_EXHAUSTED,
            detail="Freeform generation failed; a matching template is available.",
            should_continue=True,
            target_template_id=request.matching_template_id,
            trace_id=request.trace_id or report.trace_id,
            metadata=_semantic_metadata(request, report),
        )

    if _should_fallback_for_high_complexity(request):
        return _fallback_to_provider(request, report)
    if _should_fallback_for_repeated_semantic_failure(request):
        return _fallback_to_provider(
            request,
            report,
            reason=DecisionReason.PROVIDER_REPEATED_SEMANTIC_FAILURE,
            detail="Local provider repeatedly failed semantic validation.",
        )
    if _is_provider_fallback_candidate(request):
        blocked = _blocked_provider_fallback(request, report)
        if blocked is not None:
            return blocked

    return _fail(
        request,
        reason=DecisionReason.SEMANTIC_RETRY_EXHAUSTED,
        detail="Semantic validation failed and retry budget is exhausted.",
        report=report,
    )


def _budget_exceeded(request: DecisionRequest) -> bool:
    if (
        request.estimated_cost_usd is not None
        and request.cost_budget_usd is not None
        and request.estimated_cost_usd > request.cost_budget_usd
    ):
        return True
    return (
        request.elapsed_ms is not None
        and request.timeout_budget_ms is not None
        and request.elapsed_ms > request.timeout_budget_ms
    )


def _quota_exceeded(request: DecisionRequest) -> bool:
    return request.quota_remaining is not None and request.quota_remaining <= 0


def _is_mock_provider(provider: str) -> bool:
    return provider.strip().lower() == "mock"


def _provider_unavailable_decision(request: DecisionRequest) -> DecisionResult:
    if _can_fallback_to_provider(request):
        return _fallback_to_provider(
            request,
            reason=DecisionReason.PROVIDER_UNAVAILABLE,
            detail="Current provider is unavailable; explicit fallback is selected.",
        )
    if request.fallback_provider:
        blocked = _blocked_provider_fallback(request)
        if blocked is not None:
            return blocked
    return _fail(
        request,
        reason=DecisionReason.PROVIDER_UNAVAILABLE,
        detail="Current provider is unavailable and no fallback provider is configured.",
    )


def _requires_clarification(request: DecisionRequest) -> bool:
    if request.missing_required_dimensions:
        return True
    return bool(request.requirements and request.requirements.needs_clarification)


def _clarifying_questions(request: DecisionRequest) -> tuple[str, ...]:
    if request.requirements and request.requirements.clarifying_questions:
        return request.requirements.clarifying_questions[:3]
    if request.missing_required_dimensions:
        joined = ", ".join(request.missing_required_dimensions[:3])
        return (f"Please provide the required dimension(s): {joined}.",)
    return ("Please provide the missing required dimensions.",)


def _attempts_remain(request: DecisionRequest) -> bool:
    return request.attempt_index + 1 < request.max_attempts


def _provider_policy_allows_fallback(request: DecisionRequest) -> bool:
    return request.provider_policy in {
        ProviderPolicy.FALLBACK_ALLOWED,
        ProviderPolicy.BYOK_ALLOWED,
        ProviderPolicy.MANAGED_PREMIUM_ALLOWED,
    }


def _can_fallback_to_provider(request: DecisionRequest) -> bool:
    return (
        request.fallback_provider is not None
        and request.fallback_provider_available
        and _provider_policy_allows_fallback(request)
        and request.max_provider_fallbacks > 0
    )


def _is_provider_fallback_candidate(request: DecisionRequest) -> bool:
    return (
        request.fallback_provider is not None
        and _is_local_provider(request.provider)
        and (request.high_complexity or request.semantic_failure_count > 0)
    )


def _blocked_provider_fallback(
    request: DecisionRequest,
    report: SemanticValidationReport | None = None,
) -> DecisionResult | None:
    if not request.fallback_provider_available:
        return _fail(
            request,
            reason=DecisionReason.FALLBACK_PROVIDER_UNAVAILABLE,
            detail="Fallback provider is configured but unavailable.",
            report=report,
        )
    if not _provider_policy_allows_fallback(request):
        return _fail(
            request,
            reason=DecisionReason.PROVIDER_POLICY_BLOCKED,
            detail="Provider policy blocks network fallback.",
            report=report,
        )
    if request.max_provider_fallbacks <= 0:
        return _fail(
            request,
            reason=DecisionReason.PROVIDER_POLICY_BLOCKED,
            detail="Provider fallback budget is exhausted.",
            report=report,
        )
    return None


def _should_fallback_for_high_complexity(request: DecisionRequest) -> bool:
    fallback_provider = request.fallback_provider
    return (
        request.high_complexity
        and _is_local_provider(request.provider)
        and _can_fallback_to_provider(request)
        and fallback_provider is not None
        and fallback_provider.strip().lower() != request.provider.strip().lower()
    )


def _should_fallback_for_repeated_semantic_failure(request: DecisionRequest) -> bool:
    fallback_provider = request.fallback_provider
    return (
        request.semantic_failure_count > 0
        and _is_local_provider(request.provider)
        and _can_fallback_to_provider(request)
        and fallback_provider is not None
        and fallback_provider.strip().lower() != request.provider.strip().lower()
    )


def _should_fallback_to_template(request: DecisionRequest) -> bool:
    return (
        request.matching_template_id is not None
        and request.generation_mode.strip().lower().startswith("freeform")
    )


def _is_local_provider(provider: str) -> bool:
    normalized = provider.strip().lower().replace("-", "_").replace(":", "_")
    return normalized in _LOCAL_PROVIDERS or normalized.startswith("ollama_")


def _fallback_to_provider(
    request: DecisionRequest,
    report: SemanticValidationReport | None = None,
    *,
    reason: DecisionReason = DecisionReason.PROVIDER_HIGH_COMPLEXITY,
    detail: str = "Local provider appears insufficient for this request.",
) -> DecisionResult:
    return DecisionResult(
        action=DecisionAction.FALLBACK_TO_PROVIDER,
        reason=reason,
        detail=detail,
        should_continue=True,
        target_provider=request.fallback_provider,
        trace_id=request.trace_id or (report.trace_id if report else None),
        metadata=_semantic_metadata(request, report)
        if report is not None
        else _base_metadata(request),
    )


def _fail(
    request: DecisionRequest,
    *,
    reason: DecisionReason,
    detail: str,
    report: SemanticValidationReport | None = None,
) -> DecisionResult:
    return DecisionResult(
        action=DecisionAction.FAIL_WITH_REASON,
        reason=reason,
        detail=detail,
        should_continue=False,
        trace_id=request.trace_id or (report.trace_id if report else None),
        metadata=_semantic_metadata(request, report)
        if report is not None
        else _base_metadata(request),
    )


def _base_metadata(request: DecisionRequest) -> dict[str, object]:
    metadata: dict[str, object] = {
        "session_id": request.session_id,
        "subtask_id": request.subtask_id,
        "provider": request.provider,
        "generation_mode": request.generation_mode,
        "attempt_index": request.attempt_index,
        "max_attempts": request.max_attempts,
        "provider_policy": request.provider_policy.value,
        "plan_tier": request.plan_tier,
        "fallback_provider": request.fallback_provider,
        "fallback_model": request.fallback_model,
        "fallback_provider_available": request.fallback_provider_available,
        "quota_remaining": request.quota_remaining,
        "semantic_failure_count": request.semantic_failure_count,
        "max_provider_fallbacks": request.max_provider_fallbacks,
        "estimated_cost_usd": request.estimated_cost_usd,
        "cost_budget_usd": request.cost_budget_usd,
    }
    metadata.update(request.metadata)
    return metadata


def _semantic_metadata(
    request: DecisionRequest,
    report: SemanticValidationReport,
) -> dict[str, object]:
    metadata = _base_metadata(request)
    metadata.update(
        {
            "semantic_passed": report.passed,
            "missing_features": report.missing_features,
            "violated_constraints": report.violated_constraints,
            "retry_hint": report.retry_hint,
        }
    )
    return metadata


__all__ = ["RuleBasedDecisionEngine"]
