"""Public API schemas for the orchestrator (DESIGN.md §4.2).

The public API is the only stable contract — internal modules (planner,
cad generators, validator, slicer) keep evolving but these models pin
the wire format UI/CLI clients depend on. Treat any field change here
as a breaking change.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SessionState(StrEnum):
    """Lifecycle states emitted by the orchestrator.

    States transition forward only; a terminal state (``COMPLETED`` /
    ``FAILED``) never returns to ``RUNNING``. UIs can render the value
    directly.
    """

    CREATED = "created"
    PLANNING = "planning"
    AWAITING_USER_INPUT = "awaiting_user_input"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ApprovalDecision(StrEnum):
    """Outcome of a human approval gate (DESIGN.md §P9 — human-in-the-loop)."""

    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalStatus(StrEnum):
    """Lifecycle of a single ``ApprovalGate``."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ClarificationStatus(StrEnum):
    """Lifecycle of one ask-user clarification gate."""

    PENDING = "pending"
    ANSWERED = "answered"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ClarificationAnswerType(StrEnum):
    """Small input vocabulary for clarification answers."""

    TEXT = "text"
    NUMBER = "number"
    CHOICE = "choice"


class IntakeChoiceAction(StrEnum):
    """Explicit user action for an asset intake decision.

    Phase 12V deliberately records a user choice without mutating the runtime
    asset catalog or starting CAD/STL/Orca execution.
    """

    USE_EXISTING_RUNTIME_ASSET = "use_existing_runtime_asset"
    REVIEW_EXISTING_DRAFT = "review_existing_draft"
    ADAPT_SIMILAR_CANDIDATE = "adapt_similar_candidate"
    CREATE_NEW_DRAFT = "create_new_draft"
    ASK_FOR_MORE_INFO = "ask_for_more_info"


class IntakeNextStepStatus(StrEnum):
    """Safe boundary opened after an explicit intake choice."""

    RUNTIME_ASSET_SELECTED = "runtime_asset_selected"
    DRAFT_REVIEW_HANDOFF = "draft_review_handoff"
    SIMILAR_CANDIDATE_REVIEW = "similar_candidate_review"
    DRAFT_AUTHORING_STARTED = "draft_authoring_started"
    INFORMATION_NEEDED = "information_needed"


class SessionCreateRequest(BaseModel):
    """Body for ``POST /session``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    client_label: str | None = Field(default=None, max_length=120)


class SessionCreateResponse(BaseModel):
    """Response for ``POST /session``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    state: SessionState
    created_at: datetime


class ChatRequest(BaseModel):
    """Body for ``POST /chat``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1)
    message: str = Field(min_length=1, max_length=4000)


class PendingGateView(BaseModel):
    """Approval gate visible to the client."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gate_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    requested_at: datetime
    status: ApprovalStatus


class ClarificationQuestion(BaseModel):
    """A user-facing question produced by the decision layer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    field: str = Field(min_length=1)
    question: str = Field(min_length=1)
    required: bool = True
    answer_type: ClarificationAnswerType = ClarificationAnswerType.TEXT
    choices: tuple[str, ...] = ()
    unit: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _choice_questions_need_choices(self) -> "ClarificationQuestion":
        if self.answer_type is ClarificationAnswerType.CHOICE and not self.choices:
            raise ValueError("choice clarification questions require choices.")
        return self


class PendingClarificationView(BaseModel):
    """Clarification gate visible to the client."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    clarification_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    subtask_id: str | None = Field(default=None, min_length=1)
    trace_id: str | None = Field(default=None, min_length=1)
    reason: str = Field(min_length=1)
    questions: tuple[ClarificationQuestion, ...] = Field(min_length=1, max_length=3)
    source_decision: dict[str, Any] = Field(default_factory=dict)
    plan_snapshot_id: str | None = Field(default=None, min_length=1)
    requirement_snapshot_id: str | None = Field(default=None, min_length=1)
    requested_at: datetime
    status: ClarificationStatus


class SubtaskView(BaseModel):
    """Planner-emitted subtask projected for the client."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    kind: Literal["mechanical", "organic"]
    description: str = Field(min_length=1)
    depends_on: tuple[str, ...] = ()


class AssetIntakeDecisionView(BaseModel):
    """User-facing asset intake decision before draft authoring."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    runtime_asset_id: str | None = Field(default=None, min_length=1)
    draft_asset_id: str | None = Field(default=None, min_length=1)
    candidate_asset_ids: tuple[str, ...] = ()
    reference_candidate_ids: tuple[str, ...] = ()
    new_draft_allowed: bool
    metadata: dict[str, Any] = Field(default_factory=dict)


class LLMAssistShadowView(BaseModel):
    """Advisory local LLM shadow candidate exposed for comparison only.

    This view never changes product behavior. ``used_for_decision`` is always
    ``False`` and ``deterministic_route`` mirrors the actual product route.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: str = Field(min_length=1)
    succeeded: bool
    used_for_decision: bool
    model_name: str = Field(min_length=1)
    provider_name: str = Field(min_length=1)
    prompt_hash: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    deterministic_route: str = Field(min_length=1)
    confidence: float | None = None
    fallback_reason: str | None = None
    candidate_route: str | None = None
    candidate_subjects: tuple[str, ...] = ()
    candidate_style: str | None = None
    clarification_question: str | None = None
    requirement_candidate: dict[str, Any] | None = None
    search_expansion: dict[str, Any] | None = None
    raw_response_excerpt: str | None = None


class NaturalLanguageRouteView(BaseModel):
    """Beginner prompt route decision exposed before runtime generation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    selected_route: str = Field(min_length=1)
    route_snapshot_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    request_id: str | None = Field(default=None, min_length=1)
    runtime_asset_id: str | None = Field(default=None, min_length=1)
    draft_asset_id: str | None = Field(default=None, min_length=1)
    candidate_asset_ids: tuple[str, ...] = ()
    clarification_required: bool
    clarification_questions: tuple[str, ...] = ()
    generation_allowed: bool
    requirement: dict[str, Any] = Field(default_factory=dict)
    intake_decision: AssetIntakeDecisionView | None = None
    shadow_result: LLMAssistShadowView | None = None


class IntakeChoiceRequest(BaseModel):
    """Body for ``POST /intake-choice``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1)
    action: IntakeChoiceAction
    route_snapshot_id: str = Field(min_length=1)


class IntakeChoiceResponse(BaseModel):
    """Response for ``POST /intake-choice``.

    The response is intentionally audit-heavy: it tells the UI what was
    accepted while preserving the Phase 12 boundary that no product/release
    approval and no runtime execution happened merely because the choice was
    recorded.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1)
    accepted: bool
    action: IntakeChoiceAction
    state: SessionState
    route_snapshot_id: str = Field(min_length=1)
    intake_status: str | None = None
    selected_route: str | None = None
    runtime_asset_id: str | None = None
    draft_asset_id: str | None = None
    candidate_asset_ids: tuple[str, ...] = ()
    reference_candidate_ids: tuple[str, ...] = ()
    draft_authoring_requested: bool = False
    runtime_execution_started: bool = False
    runtime_catalog_registered: bool = False
    product_ready: bool = False
    release_allowed: bool = False
    message: str = Field(min_length=1)


class IntakeNextStepRequest(BaseModel):
    """Body for ``POST /intake-next-step``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1)
    route_snapshot_id: str = Field(min_length=1)


class IntakeNextStepView(BaseModel):
    """Auditable next boundary derived from a recorded intake choice."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1)
    action: IntakeChoiceAction
    status: IntakeNextStepStatus
    state: SessionState
    route_snapshot_id: str = Field(min_length=1)
    selected_route: str | None = None
    runtime_asset_id: str | None = None
    draft_asset_id: str | None = None
    candidate_asset_ids: tuple[str, ...] = ()
    reference_candidate_ids: tuple[str, ...] = ()
    draft_asset: dict[str, Any] | None = None
    draft_authoring_status: str | None = None
    draft_authoring_reason: str | None = None
    # fallback ADR item 6: structured IP/abuse pre-filter decision (decision /
    # matched_rule_id / reason_code / user_message_ko) when the capable model was
    # blocked BEFORE being invoked. Optional additive field: existing responses
    # now serialize `"prefilter": null`.
    prefilter: dict[str, Any] | None = None
    review_required: bool = True
    runtime_execution_started: bool = False
    runtime_catalog_registered: bool = False
    product_ready: bool = False
    release_allowed: bool = False
    message: str = Field(min_length=1)


class RuntimeAssetExecutionRequest(BaseModel):
    """Body for ``POST /runtime-asset-execution``.

    This starts manufacturing-runtime verification only after a prior
    ``runtime_asset_selected`` next-step boundary. Choosing an intake action
    alone never starts CAD/STL/Orca execution.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1)
    route_snapshot_id: str = Field(min_length=1)


class RuntimeAssetExecutionResponse(BaseModel):
    """Result of explicitly executing a selected curated runtime asset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1)
    state: SessionState
    route_snapshot_id: str = Field(min_length=1)
    runtime_asset_id: str = Field(min_length=1)
    subtask_id: str = Field(min_length=1)
    runtime_execution_started: bool = True
    runtime_catalog_registered: bool = False
    product_ready: bool = False
    release_allowed: bool = False
    next_step: IntakeNextStepView
    manifest: dict[str, Any]
    message: str = Field(min_length=1)


class ChatResponse(BaseModel):
    """Response for ``POST /chat``.

    Exactly one of ``subtasks`` / ``clarifying_questions`` is populated
    when the planner returns successfully. ``state`` is the post-call
    session state so the client does not need a follow-up GET.

    A pre-filter block is a *fourth* shape: every planner output field stays
    empty and ``prefilter`` carries the verdict. That combination is already
    legal — the validator below only rejects more than one populated field, so
    zero needs no exemption.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    state: SessionState
    subtasks: tuple[SubtaskView, ...] = ()
    clarifying_questions: tuple[str, ...] = ()
    pending_gate: PendingGateView | None = None
    pending_clarification: PendingClarificationView | None = None
    route_result: NaturalLanguageRouteView | None = None
    # fallback ADR item 6: structured IP/abuse pre-filter decision (decision /
    # matched_rule_id / reason_code / user_message_ko) when the raw ``/chat``
    # message was blocked BEFORE the planner was invoked. Mirrors
    # ``IntakeNextStepView.prefilter`` so both blocked boundaries carry the
    # verdict in the same shape. Optional additive field: existing responses
    # now serialize ``"prefilter": null``.
    #
    # Deliberately NOT folded into ``clarifying_questions``: "we need more
    # information" and "we do not accept this request" are different events,
    # and a client that conflates them shows a re-ask UI for a refusal.
    prefilter: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _exactly_one_planner_output(self) -> "ChatResponse":
        populated = sum(
            1
            for has_value in (
                bool(self.subtasks),
                bool(self.clarifying_questions),
                self.pending_clarification is not None,
            )
            if has_value
        )
        if populated > 1:
            raise ValueError(
                "ChatResponse may not carry subtasks, clarifying_questions, and "
                "pending_clarification in the same reply; planner must commit "
                "to one."
            )
        return self


class ApproveRequest(BaseModel):
    """Body for ``POST /approve``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1)
    gate_id: str = Field(min_length=1)
    decision: ApprovalDecision
    comments: str | None = Field(default=None, max_length=2000)


class ApproveJobRequest(BaseModel):
    """Body for ``POST /approve/{job_id}``.

    Phase 6 treats ``job_id`` as the approval gate id. A later durable job
    model can map the path id to the gate without changing this public route.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1)
    decision: ApprovalDecision
    comments: str | None = Field(default=None, max_length=2000)


class ApproveResponse(BaseModel):
    """Response for ``POST /approve``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1)
    gate_id: str = Field(min_length=1)
    state: SessionState
    decision: ApprovalDecision


ClarificationAnswerValue = str | int | float


class ClarifyRequest(BaseModel):
    """Body for ``POST /clarify``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1)
    clarification_id: str = Field(min_length=1)
    answers: dict[str, ClarificationAnswerValue] = Field(min_length=1)


class ClarifyResponse(BaseModel):
    """Response for ``POST /clarify``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1)
    clarification_id: str = Field(min_length=1)
    accepted: bool
    state: SessionState
    route_result: NaturalLanguageRouteView | None = None
    pending_clarification: PendingClarificationView | None = None


class SessionEventKind(StrEnum):
    """Discriminator for ``SessionEvent`` payloads streamed over WS."""

    STATE_CHANGED = "state_changed"
    NATURAL_LANGUAGE_ROUTE_SELECTED = "natural_language_route_selected"
    INTAKE_CHOICE_RECORDED = "intake_choice_recorded"
    INTAKE_NEXT_STEP_RECORDED = "intake_next_step_recorded"
    PLANNING_COMPLETED = "planning_completed"
    SUBTASK_STARTED = "subtask_started"
    SUBTASK_PROGRESS = "subtask_progress"
    SUBTASK_COMPLETED = "subtask_completed"
    SUBTASK_FAILED = "subtask_failed"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    CLARIFICATION_REQUESTED = "clarification_requested"
    CLARIFICATION_RESOLVED = "clarification_resolved"
    ERROR = "error"


class SessionEvent(BaseModel):
    """Server-pushed event delivered through ``WS /ws/{session_id}``.

    Payload schema is intentionally open (``dict[str, Any]``); UIs read
    the discriminator first, then the typed payload. Keeping payload
    open lets internal events evolve without rev-bumping the public WS
    contract.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    kind: SessionEventKind
    state: SessionState
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class SessionStateView(BaseModel):
    """Full session snapshot returned by ``GET /session/{id}``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    state: SessionState
    created_at: datetime
    pending_gates: tuple[PendingGateView, ...] = ()
    pending_clarification: PendingClarificationView | None = None
    subtasks: tuple[SubtaskView, ...] = ()
    latest_route_result: NaturalLanguageRouteView | None = None
    latest_intake_choice: IntakeChoiceResponse | None = None
    latest_intake_next_step: IntakeNextStepView | None = None


__all__ = [
    "ApprovalDecision",
    "ApprovalStatus",
    "ApproveJobRequest",
    "ApproveRequest",
    "ApproveResponse",
    "ClarificationAnswerType",
    "ClarificationAnswerValue",
    "ClarificationQuestion",
    "ClarificationStatus",
    "ClarifyRequest",
    "ClarifyResponse",
    "ChatRequest",
    "ChatResponse",
    "AssetIntakeDecisionView",
    "IntakeChoiceAction",
    "IntakeChoiceRequest",
    "IntakeChoiceResponse",
    "IntakeNextStepRequest",
    "IntakeNextStepStatus",
    "IntakeNextStepView",
    "NaturalLanguageRouteView",
    "PendingGateView",
    "PendingClarificationView",
    "RuntimeAssetExecutionRequest",
    "RuntimeAssetExecutionResponse",
    "SessionCreateRequest",
    "SessionCreateResponse",
    "SessionEvent",
    "SessionEventKind",
    "SessionState",
    "SessionStateView",
    "SubtaskView",
]
