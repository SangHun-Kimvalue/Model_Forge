from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from modules.requirements.schemas import RequirementSpec
from modules.semantic_validator.schemas import SemanticValidationReport


class DecisionAction(StrEnum):
    """Next action selected by the orchestrator decision layer."""

    PROCEED_TO_SLICE = "proceed_to_slice"
    RETRY_WITH_SELF_HEALER = "retry_with_self_healer"
    FALLBACK_TO_PROVIDER = "fallback_to_provider"
    FALLBACK_TO_TEMPLATE = "fallback_to_template"
    ASK_USER_CLARIFICATION = "ask_user_clarification"
    FAIL_WITH_REASON = "fail_with_reason"


class DecisionReason(StrEnum):
    """Machine-readable reason for a decision action."""

    SEMANTIC_PASSED = "semantic_passed"
    SEMANTIC_MISSING_FEATURES = "semantic_missing_features"
    SEMANTIC_RETRY_EXHAUSTED = "semantic_retry_exhausted"
    SEMANTIC_REPORT_MISSING = "semantic_report_missing"
    MISSING_REQUIRED_DIMENSION = "missing_required_dimension"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_POLICY_BLOCKED = "provider_policy_blocked"
    PROVIDER_HIGH_COMPLEXITY = "provider_high_complexity"
    PROVIDER_REPEATED_SEMANTIC_FAILURE = "provider_repeated_semantic_failure"
    FALLBACK_PROVIDER_UNAVAILABLE = "fallback_provider_unavailable"
    BUDGET_EXCEEDED = "budget_exceeded"
    QUOTA_EXCEEDED = "quota_exceeded"
    APPROVAL_SNAPSHOT_MISMATCH = "approval_snapshot_mismatch"
    MOCK_PROVIDER_IN_PRODUCT_PATH = "mock_provider_in_product_path"


class ProviderPolicy(StrEnum):
    """Plan/provider policy that constrains network fallback decisions."""

    LOCAL_ONLY = "local_only"
    FALLBACK_ALLOWED = "fallback_allowed"
    BYOK_ALLOWED = "byok_allowed"
    MANAGED_PREMIUM_ALLOWED = "managed_premium_allowed"


class DecisionRequest(BaseModel):
    """Signals used to choose the next pipeline action.

    The request is intentionally telemetry-only. It must not contain executable
    CAD code or provider clients; the decision engine returns policy, not work.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1)
    subtask_id: str = Field(min_length=1)
    user_prompt: str = Field(min_length=1)
    requirements: RequirementSpec | None = None
    generation_mode: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    semantic_report: SemanticValidationReport | None = None
    attempt_index: int = Field(ge=0)
    max_attempts: int = Field(gt=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    cost_budget_usd: float | None = Field(default=None, ge=0)
    elapsed_ms: int | None = Field(default=None, ge=0)
    timeout_budget_ms: int | None = Field(default=None, gt=0)
    approval_snapshot_matches: bool = True
    product_mode: bool = False
    provider_available: bool = True
    fallback_provider: str | None = Field(default=None, min_length=1)
    fallback_model: str | None = Field(default=None, min_length=1)
    fallback_provider_available: bool = True
    provider_policy: ProviderPolicy = ProviderPolicy.FALLBACK_ALLOWED
    plan_tier: str | None = Field(default=None, min_length=1)
    quota_remaining: float | None = Field(default=None, ge=0)
    semantic_failure_count: int = Field(default=0, ge=0)
    max_provider_fallbacks: int = Field(default=1, ge=0)
    high_complexity: bool = False
    matching_template_id: str | None = Field(default=None, min_length=1)
    missing_required_dimensions: tuple[str, ...] = ()
    trace_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DecisionResult(BaseModel):
    """Deterministic policy decision returned by the decision layer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: DecisionAction
    reason: DecisionReason
    detail: str = Field(min_length=1)
    should_continue: bool
    target_provider: str | None = None
    target_template_id: str | None = None
    questions: tuple[str, ...] = ()
    next_attempt: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None

    @model_validator(mode="after")
    def validate_action_payload(self) -> DecisionResult:
        continuing_actions = {
            DecisionAction.PROCEED_TO_SLICE,
            DecisionAction.RETRY_WITH_SELF_HEALER,
            DecisionAction.FALLBACK_TO_PROVIDER,
            DecisionAction.FALLBACK_TO_TEMPLATE,
        }
        if self.action in continuing_actions:
            if not self.should_continue:
                raise ValueError(f"{self.action} requires should_continue=True.")
        elif self.should_continue:
            raise ValueError(f"{self.action} requires should_continue=False.")

        if self.action == DecisionAction.FALLBACK_TO_PROVIDER:
            if self.target_provider is None:
                raise ValueError("fallback_to_provider requires target_provider.")
        else:
            if self.target_provider is not None:
                raise ValueError("target_provider is only valid for provider fallback.")

        if self.action == DecisionAction.FALLBACK_TO_TEMPLATE:
            if self.target_template_id is None:
                raise ValueError("fallback_to_template requires target_template_id.")
        else:
            if self.target_template_id is not None:
                raise ValueError("target_template_id is only valid for template fallback.")

        if self.action == DecisionAction.ASK_USER_CLARIFICATION:
            if not self.questions:
                raise ValueError("ask_user_clarification requires questions.")
        else:
            if self.questions:
                raise ValueError("questions are only valid for ask_user_clarification.")

        if self.action == DecisionAction.RETRY_WITH_SELF_HEALER:
            if self.next_attempt is None:
                raise ValueError("retry_with_self_healer requires next_attempt.")
        else:
            if self.next_attempt is not None:
                raise ValueError("next_attempt is only valid for self-healer retry.")

        return self


__all__ = [
    "DecisionAction",
    "DecisionReason",
    "DecisionRequest",
    "DecisionResult",
    "ProviderPolicy",
]
