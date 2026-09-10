"""Orchestrator graph engine — thin glue calling plugin factories.

PHASES Phase 6 explicitly notes the LangGraph dependency is internal
(T2 per §2.3); we ship an in-house ``async`` state machine that
satisfies the public ``/session`` /``/chat`` /``/approve`` /``WS``
contract. Swapping to LangGraph or Temporal later is a localized
change because every node is a plain coroutine taking ``Dependencies``
and a ``Session`` snapshot.

Node responsibilities:
- ``run_chat_turn``: planner.plan(message) → subtasks/clarifying_questions
  → emit ``PLANNING_COMPLETED`` event → if subtasks exist, register a
  human-approval gate (P9) and transition to ``AWAITING_APPROVAL``;
  otherwise the session stays in ``CREATED`` (clarifying questions only).
- ``run_approval_resolution``: resolve gate → transition to ``RUNNING``
  (approved) or ``FAILED`` (rejected). For approved gates Phase 8A
  executes the real ``cad → validator → slicer`` pipeline per subtask
  using the adapters wired in ``Dependencies``; failure of any subtask
  emits ``SUBTASK_FAILED`` and drives the session to ``FAILED``.

All nodes propagate ``trace_id`` (Phase 3 ``bind_trace``) so log lines
emitted from downstream plugins carry the orchestrator's trace id.

Phase 9D notes:
- ``mechanical`` subtasks run the CAD → semantic gate → validate → slice
  pipeline.
- ``organic`` subtasks run the organic generator and emit manifest-backed mesh
  artifacts; mechanical/organic merge policy remains a later ADR.

Phase 8B notes:
- ``cad_coder`` (DESIGN.md §4.2.cad_coder) is wired through
  ``MechanicalPipeline``. The mock adapter emits a deterministic
  ``cadquery`` snippet so downstream stages stay reproducible. Replacing
  the mock with a real LLM-backed coder is a localized swap via
  ``CAD_CODER_AGENT_ADAPTER``.
"""

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from apps.orchestrator.dependencies import Dependencies, OrchestratorSettings
from apps.orchestrator.exceptions import (
    ClarificationRequiredError,
    OrchestratorConfigError,
    PrefilterBlockedError,
    SessionStateError,
)
from apps.orchestrator.pipeline import (
    MechanicalPipeline,
    OrganicPipeline,
    PipelineProgress,
)
from apps.orchestrator.schemas import (
    ApprovalDecision,
    AssetIntakeDecisionView,
    ChatResponse,
    ClarificationAnswerValue,
    ClarificationQuestion,
    ClarifyResponse,
    IntakeChoiceAction,
    IntakeChoiceRequest,
    IntakeChoiceResponse,
    IntakeNextStepRequest,
    IntakeNextStepStatus,
    IntakeNextStepView,
    LLMAssistShadowView,
    NaturalLanguageRouteView,
    PendingGateView,
    SessionEventKind,
    SessionState,
    SubtaskView,
)
from apps.orchestrator.sessions import (
    Session,
    commit_route_clarification,
    make_event,
    pending_clarification_view,
    prepare_clarification_resolution,
    prepare_route_clarification,
    register_clarification,
    register_gate,
    register_route_clarification,
    resolve_clarification,
    resolve_gate,
    transition,
)
from modules.agents.planner.exceptions import PlannerAgentError
from modules.agents.planner.schemas import PlannerRequest, PlannerResponse
from modules.artifacts import get_artifact_root, subtask_dir, write_failure_manifest
from modules.newbie_request.asset_authoring import (
    CAPABLE_PREFILTER_BLOCKED_REASONS,
    CAPABLE_PREFILTER_METADATA_KEY,
)
from modules.newbie_request.asset_draft_schemas import (
    MANUAL_REVIEW_IP_DECISION,
    DraftAssetAuthoringRequest,
    DraftAssetAuthoringResult,
    DraftAssetAuthoringStatus,
    PrefilterManualReviewRecord,
)
from modules.newbie_request.llm_assist import (
    default_shadow_context,
    should_run_clarifier_shadow,
)
from modules.newbie_request.natural_language import (
    NaturalLanguageRouteResult,
    NewbieRequirementExtraction,
    RequirementExtractionReason,
)
from modules.observability.trace import bind_trace
from modules.slicer.schemas import MaterialPreset, PrinterProfile

_INTAKE_STATUS_TO_ACTION: dict[str, IntakeChoiceAction] = {
    "runtime_catalog_match": IntakeChoiceAction.USE_EXISTING_RUNTIME_ASSET,
    "draft_queue_match": IntakeChoiceAction.REVIEW_EXISTING_DRAFT,
    "duplicate_or_similar_candidate": IntakeChoiceAction.ADAPT_SIMILAR_CANDIDATE,
    "new_draft_allowed": IntakeChoiceAction.CREATE_NEW_DRAFT,
}

_INTAKE_CHOICE_MESSAGE: dict[IntakeChoiceAction, str] = {
    IntakeChoiceAction.USE_EXISTING_RUNTIME_ASSET: (
        "기존 검증 후보 사용 선택을 기록했습니다. 시각 품질과 출시 검토는 아직 필요합니다."
    ),
    IntakeChoiceAction.REVIEW_EXISTING_DRAFT: (
        "기존 에셋 초안 검토 선택을 기록했습니다. 새 초안은 만들지 않습니다."
    ),
    IntakeChoiceAction.ADAPT_SIMILAR_CANDIDATE: (
        "유사 후보를 재사용 또는 수정 검토하는 선택을 기록했습니다. 새 초안은 만들지 않습니다."
    ),
    IntakeChoiceAction.CREATE_NEW_DRAFT: (
        "새 에셋 초안 생성 요청을 기록했습니다. "
        "제품 카탈로그 등록과 출시 승인은 별도 검토가 필요합니다."
    ),
    IntakeChoiceAction.ASK_FOR_MORE_INFO: "추가 정보 요청 선택을 기록했습니다.",
}

_INTAKE_NEXT_STEP_STATUS: dict[IntakeChoiceAction, IntakeNextStepStatus] = {
    IntakeChoiceAction.USE_EXISTING_RUNTIME_ASSET: (
        IntakeNextStepStatus.RUNTIME_ASSET_SELECTED
    ),
    IntakeChoiceAction.REVIEW_EXISTING_DRAFT: IntakeNextStepStatus.DRAFT_REVIEW_HANDOFF,
    IntakeChoiceAction.ADAPT_SIMILAR_CANDIDATE: (
        IntakeNextStepStatus.SIMILAR_CANDIDATE_REVIEW
    ),
    IntakeChoiceAction.CREATE_NEW_DRAFT: IntakeNextStepStatus.DRAFT_AUTHORING_STARTED,
    IntakeChoiceAction.ASK_FOR_MORE_INFO: IntakeNextStepStatus.INFORMATION_NEEDED,
}

_INTAKE_NEXT_STEP_MESSAGE: dict[IntakeNextStepStatus, str] = {
    IntakeNextStepStatus.RUNTIME_ASSET_SELECTED: (
        "기존 런타임 후보가 선택되었습니다. 아직 CAD/STL/Orca 실행은 시작하지 않습니다."
    ),
    IntakeNextStepStatus.DRAFT_REVIEW_HANDOFF: (
        "기존 초안 검토 단계로 넘겼습니다. 새 초안은 만들지 않습니다."
    ),
    IntakeNextStepStatus.SIMILAR_CANDIDATE_REVIEW: (
        "유사 후보 재사용/수정 검토 단계로 넘겼습니다. 새 초안은 만들지 않습니다."
    ),
    IntakeNextStepStatus.DRAFT_AUTHORING_STARTED: (
        "새 에셋 초안이 생성되었습니다. 검토 대기 상태이며 출시 승인은 아닙니다."
    ),
    IntakeNextStepStatus.INFORMATION_NEEDED: (
        "추가 정보가 필요합니다. 질문 답변은 clarification 흐름에서 진행합니다."
    ),
}


def _printer_from_settings(settings: OrchestratorSettings) -> PrinterProfile:
    """Build the slicing printer profile from orchestrator-level defaults."""
    return PrinterProfile(
        name=settings.default_printer_profile,
        nozzle_diameter_mm=settings.default_nozzle_diameter_mm,
        bed_size_mm=settings.default_bed_size_mm,
    )


def _material_from_settings(settings: OrchestratorSettings) -> MaterialPreset:
    """Build the slicing material preset from orchestrator-level defaults."""
    return MaterialPreset(
        name=settings.default_material_profile,
        nozzle_temp_c=settings.default_nozzle_temp_c,
        bed_temp_c=settings.default_bed_temp_c,
    )


@dataclass(frozen=True)
class ApprovalExecutionPlan:
    """Immediate result of resolving a human approval gate."""

    approved: bool
    approved_subtasks: tuple[SubtaskView, ...]
    gate_id: str | None = None
    plan_snapshot_id: str | None = None

    @property
    def should_execute(self) -> bool:
        return self.approved


@dataclass(frozen=True)
class ClarificationExecutionPlan:
    """Immediate result of resolving a clarification gate."""

    clarification_id: str
    resume_subtasks: tuple[SubtaskView, ...]
    approval_gate_id: str | None = None
    plan_snapshot_id: str | None = None

    @property
    def should_execute(self) -> bool:
        return bool(self.resume_subtasks)


async def _invoke_planner(
    deps: Dependencies, session: Session, message: str
) -> PlannerResponse:
    """Call the planner inside the session's trace context.

    Wraps ``PlannerAgentError`` in a graph-level ``SessionStateError``
    only when the failure mode means the session cannot continue. P8
    keeps the original exception chained for log/trace correlation.
    """
    request = PlannerRequest(
        prompt=message,
        session_id=session.session_id,
        trace_id=session.trace_id,
    )
    return await deps.planner.plan(request)


def _subtasks_to_views(planner_output: PlannerResponse) -> tuple[SubtaskView, ...]:
    return tuple(
        SubtaskView(
            id=st.id,
            kind=st.kind,
            description=st.description,
            depends_on=tuple(st.depends_on),
        )
        for st in planner_output.subtasks
    )


def _route_snapshot_id(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _route_to_view(
    route: NaturalLanguageRouteResult,
    *,
    route_revision: int,
) -> NaturalLanguageRouteView:
    intake_decision = (
        AssetIntakeDecisionView(
            **route.intake_decision.model_dump(mode="json"),
        )
        if route.intake_decision is not None
        else None
    )
    payload: dict[str, object] = {
        "selected_route": route.selected_route.value,
        "reason": route.reason.value,
        "request_id": route.request_id,
        "runtime_asset_id": route.runtime_asset_id,
        "draft_asset_id": route.draft_asset_id,
        "candidate_asset_ids": route.candidate_asset_ids,
        "clarification_required": route.clarification_required,
        "clarification_questions": route.clarification_questions,
        "generation_allowed": route.generation_allowed,
        "requirement": route.requirement.model_dump(mode="json"),
        "intake_decision": intake_decision.model_dump(mode="json")
        if intake_decision is not None
        else None,
    }
    snapshot_payload = {**payload, "route_revision": route_revision}
    return NaturalLanguageRouteView(
        route_snapshot_id=_route_snapshot_id(snapshot_payload),
        shadow_result=_shadow_to_view(route),
        **payload,
    )


def _shadow_to_view(
    route: NaturalLanguageRouteResult,
) -> LLMAssistShadowView | None:
    if route.shadow_result is None:
        return None
    return LLMAssistShadowView(**route.shadow_result.model_dump(mode="json"))


async def _attach_shadow_result(
    deps: Dependencies,
    message: str,
    route_result: NaturalLanguageRouteResult,
) -> NaturalLanguageRouteResult:
    """Record an advisory local LLM shadow candidate without changing behavior.

    Shadow mode is opt-in. When disabled or unconfigured, the deterministic
    route is returned unchanged. The shadow result is attached as advisory
    metadata only; it never alters ``selected_route``, ``generation_allowed``,
    intake, or any gate state.
    """

    if not deps.settings.llm_assist_shadow_enabled:
        return route_result
    facade = deps.llm_assist_facade
    if facade is None:
        return route_result
    if not should_run_clarifier_shadow(route_result):
        return route_result
    shadow = await facade.clarify_requirement_shadow(
        message,
        route_result,
        default_shadow_context(),
    )
    return route_result.model_copy(update={"shadow_result": shadow})


def _route_questions(
    route: NaturalLanguageRouteResult,
) -> tuple[ClarificationQuestion, ...]:
    return tuple(
        ClarificationQuestion(
            id=f"q{index}",
            field="route_clarification",
            question=question,
        )
        for index, question in enumerate(route.clarification_questions, start=1)
    )


def _route_event_payload(route_view: NaturalLanguageRouteView) -> dict[str, object]:
    return route_view.model_dump(mode="json")


def _is_unknown_decorative_subject_gate(
    source_decision: dict[str, object],
) -> bool:
    requirement = source_decision.get("requirement")
    return (
        isinstance(requirement, dict)
        and requirement.get("category") == "decorative_keyring"
        and requirement.get("clarification_reason")
        == RequirementExtractionReason.UNKNOWN_DECORATIVE_SUBJECT.value
    )


def _allowed_intake_action(route: NaturalLanguageRouteView) -> IntakeChoiceAction:
    if route.intake_decision is None:
        if route.clarification_required or route.selected_route == "ask_user":
            return IntakeChoiceAction.ASK_FOR_MORE_INFO
        raise SessionStateError(
            "Latest route result does not carry an actionable intake decision."
        )
    try:
        return _INTAKE_STATUS_TO_ACTION[route.intake_decision.status]
    except KeyError as exc:
        raise SessionStateError(
            f"Unsupported intake status: {route.intake_decision.status!r}."
        ) from exc


def _intake_choice_response(
    session: Session,
    route: NaturalLanguageRouteView,
    action: IntakeChoiceAction,
) -> IntakeChoiceResponse:
    intake = route.intake_decision
    return IntakeChoiceResponse(
        session_id=session.session_id,
        accepted=True,
        action=action,
        state=session.state,
        route_snapshot_id=route.route_snapshot_id,
        intake_status=intake.status if intake is not None else None,
        selected_route=route.selected_route,
        runtime_asset_id=intake.runtime_asset_id
        if intake is not None
        else route.runtime_asset_id,
        draft_asset_id=intake.draft_asset_id
        if intake is not None
        else route.draft_asset_id,
        candidate_asset_ids=intake.candidate_asset_ids
        if intake is not None
        else route.candidate_asset_ids,
        reference_candidate_ids=intake.reference_candidate_ids
        if intake is not None
        else (),
        draft_authoring_requested=action is IntakeChoiceAction.CREATE_NEW_DRAFT,
        runtime_execution_started=False,
        runtime_catalog_registered=False,
        product_ready=False,
        release_allowed=False,
        message=_INTAKE_CHOICE_MESSAGE[action],
    )


def _string_from_requirement(route: NaturalLanguageRouteView, key: str) -> str | None:
    value = route.requirement.get(key)
    return value if isinstance(value, str) and value else None


def _authoring_request_from_route(
    route: NaturalLanguageRouteView,
) -> DraftAssetAuthoringRequest:
    category = _string_from_requirement(route, "category")
    return DraftAssetAuthoringRequest(
        user_prompt_ko=_string_from_requirement(route, "user_prompt_ko")
        or "사용자 요청",
        subject=_string_from_requirement(route, "subject"),
        category="keyring" if category == "decorative_keyring" else category,
        style=_string_from_requirement(route, "style"),
        review_queue_reason="intake_choice_create_new_draft",
    )


_PREFILTER_BLOCKED_REASONS = frozenset(CAPABLE_PREFILTER_BLOCKED_REASONS.values())


def _prefilter_from_authoring(
    authoring_result: DraftAssetAuthoringResult,
) -> dict[str, object] | None:
    """Lift the structured pre-filter decision onto the public next-step view.

    Fail-closed (fallback ADR item 6): when the authoring result carries one of the
    fixed blocked reasons, the structured decision MUST be present as a dict.
    Silently forwarding ``None`` would hide a preservation-contract violation and
    leave the follow-up manual-review routing phase unable to identify the case.
    """
    raw = authoring_result.intake_metadata.get(CAPABLE_PREFILTER_METADATA_KEY)
    if isinstance(raw, dict):
        return dict(raw)
    if authoring_result.reason in _PREFILTER_BLOCKED_REASONS:
        raise SessionStateError(
            "Capable-model pre-filter blocked the request "
            f"({authoring_result.reason}) but intake metadata carries no "
            f"structured '{CAPABLE_PREFILTER_METADATA_KEY}' dict "
            f"(got {type(raw).__name__}); refusing to drop the safety verdict."
        )
    return None


def _manual_review_record(
    *,
    deps: Dependencies,
    prefilter: dict[str, object],
    originating_prompt: str,
) -> PrefilterManualReviewRecord:
    """Build the durable record from the exact decision that blocked the call.

    Nothing is re-derived: the four decision fields come from the same
    ``prefilter`` dict handed to the response, and ``originating_prompt`` is the
    prompt the pre-filter actually judged.
    """
    return PrefilterManualReviewRecord.model_validate(
        {
            "review_id": deps.review_id_factory(),
            "created_at": deps.created_at_factory().isoformat(),
            "originating_prompt": originating_prompt,
            "decision": prefilter.get("decision"),
            "matched_rule_id": prefilter.get("matched_rule_id"),
            "reason_code": prefilter.get("reason_code"),
            "user_message_ko": prefilter.get("user_message_ko"),
        }
    )


#: Machine-readable ``prefilter_route_reason`` per blocked verdict on the raw
#: ``/chat`` planner-input path (fallback ADR item 6). Mirrors the pipeline tables
#: in ``apps.orchestrator.pipeline``: closed, and interpolating neither the
#: variable ``reason_code`` nor any human-facing text, so a consumer branches on
#: an exact string.
#:
#: These values are deliberately distinct from every mechanical / organic /
#: capable-model route reason: "which entry point refused this" is exactly what
#: an auditor needs and cannot re-derive from the verdict alone.
#:
#: This is NOT ``FallbackGateDecision.reason_code``. That field belongs to the
#: rule tables and reaches the response and the durable record verbatim;
#: overwriting it with a route reason would forge the audit trail.
PLANNER_PREFILTER_ROUTE_REASONS: Mapping[str, str] = MappingProxyType(
    {
        "blocked_prohibited": "planner_prefilter_blocked_prohibited",
        "manual_review_ip": "planner_prefilter_manual_review_ip",
    }
)


def _blocked_chat_response(
    deps: Dependencies, session: Session, message: str
) -> ChatResponse | None:
    """Run the IP/abuse guard on a raw ``/chat`` message; ``None`` means allow.

    fallback ADR item 6: the guard must run *before* the agent is invoked — "do not
    generate then catch". ``/chat`` is the last raw-NL ingress that reached an
    LLM unchecked, and the planner is not the only consumer behind it: the
    natural-language router and its opt-in clarifier shadow LLM both run earlier
    in the turn. So this is called as the first action of the turn's work, not
    merely before ``_invoke_planner``.

    Returning a response instead of raising (the pipelines' ``PrefilterBlocked
    Error`` style) is deliberate: a chat turn's refusal *is* its normal reply,
    and the caller must not have to translate an exception into one.

    An unmapped non-``allow`` verdict fails closed — a taxonomy that grew a
    value this wiring has never seen must not be read as permission.
    """
    decision = deps.prefilter(message)
    if decision.decision == "allow":
        return None
    route_reason = PLANNER_PREFILTER_ROUTE_REASONS.get(decision.decision)
    if route_reason is None:
        raise OrchestratorConfigError(
            f"Unmapped IP/abuse pre-filter verdict {decision.decision!r} in "
            "planner-input wiring; refusing to plan on an unknown safety verdict."
        )
    prefilter = decision.model_dump(mode="json")
    # Neither the response nor the manual-review record has a field for the
    # route reason, and inventing one would either be a dead constant or
    # overwrite the rule table's ``reason_code``. So the routing fact is
    # observed here instead — emitted inside the caller's ``bind_trace`` block,
    # hence bound to the turn's trace and session.
    #
    # Both blocked verdicts log. ``blocked_prohibited`` is never queued, so for
    # that verdict this line is the *only* audit trace that the request existed.
    deps.logger.warning(
        "planner.prefilter_blocked",
        extra={
            "session_id": session.session_id,
            "decision": decision.decision,
            "matched_rule_id": decision.matched_rule_id,
            # The rule table's original, not the route reason above.
            "reason_code": decision.reason_code,
            "prefilter_route_reason": route_reason,
        },
    )
    if decision.decision == MANUAL_REVIEW_IP_DECISION:
        # Persisted before the response is built, with the same contract as the
        # intake path: a storage fault propagates rather than being swallowed
        # into a reply that claims a review request exists. Unlike approved
        # execution (a background task that would strand the session at
        # RUNNING), ``/chat`` is synchronous and the session state is untouched,
        # so there is nothing to unwind and no reason to soften the failure.
        #
        # ``blocked_prohibited`` is not queued: fallback ADR asks for *block* and
        # *route to manual review* as different outcomes, and the record schema
        # refuses to model it anyway.
        deps.draft_queue.save_manual_review(
            _manual_review_record(
                deps=deps,
                prefilter=prefilter,
                originating_prompt=message,
            )
        )
    # The session stays in ``CREATED``: a refusal means "this request is not
    # accepted", not "this session is broken". Moving it to FAILED or
    # AWAITING_USER_INPUT would cost the user the ability to fix the prompt and
    # retry, since ``/chat`` is only legal from ``CREATED``.
    return ChatResponse(
        session_id=session.session_id,
        trace_id=session.trace_id,
        state=session.state,
        prefilter=prefilter,
    )


def _closed_next_step(
    *,
    session: Session,
    route: NaturalLanguageRouteView,
    choice: IntakeChoiceResponse,
    status: IntakeNextStepStatus,
    draft_asset: dict[str, object] | None = None,
    draft_authoring_status: str | None = None,
    draft_authoring_reason: str | None = None,
    prefilter: dict[str, object] | None = None,
) -> IntakeNextStepView:
    draft_asset_id = choice.draft_asset_id
    if draft_asset is not None:
        raw_draft_asset_id = draft_asset.get("draft_asset_id")
        draft_asset_id = (
            raw_draft_asset_id if isinstance(raw_draft_asset_id, str) else None
        )
    return IntakeNextStepView(
        session_id=session.session_id,
        action=choice.action,
        status=status,
        state=session.state,
        route_snapshot_id=route.route_snapshot_id,
        selected_route=route.selected_route,
        runtime_asset_id=choice.runtime_asset_id,
        draft_asset_id=draft_asset_id,
        candidate_asset_ids=choice.candidate_asset_ids,
        reference_candidate_ids=choice.reference_candidate_ids,
        draft_asset=draft_asset,
        draft_authoring_status=draft_authoring_status,
        draft_authoring_reason=draft_authoring_reason,
        prefilter=prefilter,
        review_required=True,
        runtime_execution_started=False,
        runtime_catalog_registered=False,
        product_ready=False,
        release_allowed=False,
        message=_INTAKE_NEXT_STEP_MESSAGE[status],
    )


def _mechanical_pipeline(deps: Dependencies, log: logging.Logger) -> MechanicalPipeline:
    return MechanicalPipeline(
        cad_coder=deps.cad_coder,
        requirement_extractor=deps.requirement_extractor,
        semantic_validator=deps.semantic_validator,
        decision_engine=deps.decision_engine,
        cad_coder_provider_factory=deps.cad_coder_provider_factory,
        template_fallback_router=deps.template_fallback_router,
        self_healer=deps.self_healer,
        cad=deps.cad,
        validator=deps.validator,
        slicer=deps.slicer,
        logger=log,
        # fallback ADR item 6: the deterministic IP/abuse gate the composition root
        # owns, injected so no execution pipeline can be assembled ungated.
        prefilter=deps.prefilter,
        printer=_printer_from_settings(deps.settings),
        material=_material_from_settings(deps.settings),
        process_profile=deps.settings.default_process_profile,
        cad_coder_dsl=deps.settings.cad_coder_dsl,
        semantic_max_attempts=deps.settings.semantic_max_attempts,
        product_mode=deps.settings.product_mode,
        decision_cost_budget_usd=deps.settings.decision_cost_budget_usd,
        decision_timeout_budget_ms=deps.settings.decision_timeout_budget_ms,
        decision_provider_policy=deps.settings.decision_provider_policy,
        decision_plan_tier=deps.settings.decision_plan_tier,
        decision_fallback_provider=deps.settings.decision_fallback_provider,
        decision_fallback_model=deps.settings.decision_fallback_model,
        decision_fallback_provider_available=(
            deps.settings.decision_fallback_provider_available
        ),
        decision_quota_remaining=deps.settings.decision_quota_remaining,
        decision_max_provider_fallbacks=deps.settings.decision_max_provider_fallbacks,
    )


def _organic_pipeline(deps: Dependencies, log: logging.Logger) -> OrganicPipeline:
    return OrganicPipeline(organic=deps.organic, logger=log, prefilter=deps.prefilter)


def _answers_to_description_suffix(
    questions: tuple[ClarificationQuestion, ...],
    answers: dict[str, ClarificationAnswerValue],
) -> str:
    question_lookup = {question.id: question.question for question in questions}
    lines = ["사용자 추가 답변:"]
    for question_id, value in answers.items():
        question_text = question_lookup.get(question_id, question_id)
        lines.append(f"- {question_text}: {value}")
        lowered_question = question_text.lower()
        if "구멍" in question_text or "hole" in lowered_question:
            lines.append(f"- 구멍 {value}개")
    return "\n".join(lines)


def _merge_clarification_answers(
    subtasks: tuple[SubtaskView, ...],
    *,
    subtask_id: str,
    questions: tuple[ClarificationQuestion, ...],
    answers: dict[str, ClarificationAnswerValue],
) -> tuple[SubtaskView, ...]:
    suffix = _answers_to_description_suffix(questions, answers)
    merged: list[SubtaskView] = []
    for subtask in subtasks:
        if subtask.id != subtask_id:
            merged.append(subtask)
            continue
        merged.append(
            subtask.model_copy(
                update={"description": f"{subtask.description}\n{suffix}"}
            )
        )
    return tuple(merged)


async def run_chat_turn(
    deps: Dependencies, session: Session, message: str
) -> ChatResponse:
    """One ``/chat`` turn: pre-filter → planner call → state transition → event.

    Preconditions:
        session is locked by the caller (``SessionStore.lock_for``).
        session.state is ``CREATED``. Phase 6 does not support concurrent
        plan replacement; refine/edit flows need an explicit later API.
    """

    if session.state is not SessionState.CREATED:
        raise SessionStateError(
            f"Session '{session.session_id}' is {session.state.value}; "
            "/chat is only allowed before a plan enters approval or execution."
        )

    with bind_trace(trace_id=session.trace_id, session_id=session.session_id):
        # fallback ADR item 6: the very first thing this turn does, and inside the
        # trace binding so the safety verdict's log line carries trace_id and
        # session_id like everything else on the request path.
        #
        # Ordering is the contract, not an implementation detail: the router,
        # its clarifier shadow LLM and the planner all live below this line, and
        # so does the PLANNING transition. Blocking after the transition would
        # send the user a "계획 중" signal for a request that was never planned —
        # "do not generate then catch" applied to state signals too.
        blocked = _blocked_chat_response(deps, session, message)
        if blocked is not None:
            return blocked

        previous_state = session.state
        session.state = SessionState.PLANNING
        await deps.sessions.publish(
            session.session_id,
            make_event(
                session,
                SessionEventKind.STATE_CHANGED,
                payload={"from": previous_state.value, "to": session.state.value},
            ),
        )

        try:
            route_result = (
                deps.natural_language_router.route_if_recognized(message)
                if deps.settings.natural_language_route_entry_enabled
                else None
            )
            if route_result is not None:
                route_result = await _attach_shadow_result(deps, message, route_result)
                session.route_revision += 1
                route_view = _route_to_view(
                    route_result,
                    route_revision=session.route_revision,
                )
                session.latest_route_result = route_view
                session.latest_intake_choice = None
                session.latest_intake_next_step = None
                await deps.sessions.publish(
                    session.session_id,
                    make_event(
                        session,
                        SessionEventKind.NATURAL_LANGUAGE_ROUTE_SELECTED,
                        payload=_route_event_payload(route_view),
                    ),
                )
                if route_result.clarification_required:
                    questions = _route_questions(route_result)
                    clarification_gate = register_route_clarification(
                        session,
                        questions=questions,
                        source_decision=_route_event_payload(route_view),
                        reason=route_result.reason.value,
                    )
                    transition(
                        session,
                        SessionState.AWAITING_USER_INPUT,
                        allowed_from=(SessionState.PLANNING,),
                    )
                    await deps.sessions.publish(
                        session.session_id,
                        make_event(
                            session,
                            SessionEventKind.STATE_CHANGED,
                            payload={
                                "from": SessionState.PLANNING.value,
                                "to": session.state.value,
                            },
                        ),
                    )
                    await deps.sessions.publish(
                        session.session_id,
                        make_event(
                            session,
                            SessionEventKind.CLARIFICATION_REQUESTED,
                            payload=clarification_gate.to_view(
                                session.trace_id
                            ).model_dump(mode="json"),
                        ),
                    )
                    return ChatResponse(
                        session_id=session.session_id,
                        trace_id=session.trace_id,
                        state=session.state,
                        pending_clarification=clarification_gate.to_view(
                            session.trace_id
                        ),
                        route_result=route_view,
                    )

                session.state = previous_state
                await deps.sessions.publish(
                    session.session_id,
                    make_event(
                        session,
                        SessionEventKind.STATE_CHANGED,
                        payload={
                            "from": SessionState.PLANNING.value,
                            "to": session.state.value,
                        },
                    ),
                )
                return ChatResponse(
                    session_id=session.session_id,
                    trace_id=session.trace_id,
                    state=session.state,
                    route_result=route_view,
                )

            planner_output = await _invoke_planner(deps, session, message)
        except PlannerAgentError as exc:
            session.state = SessionState.FAILED
            await deps.sessions.publish(
                session.session_id,
                make_event(
                    session,
                    SessionEventKind.STATE_CHANGED,
                    payload={
                        "from": SessionState.PLANNING.value,
                        "to": session.state.value,
                    },
                ),
            )
            await deps.sessions.publish(
                session.session_id,
                make_event(
                    session,
                    SessionEventKind.ERROR,
                    payload={"error_type": type(exc).__name__, "detail": str(exc)},
                ),
            )
            raise

        subtask_views = _subtasks_to_views(planner_output)
        session.subtasks = subtask_views

        await deps.sessions.publish(
            session.session_id,
            make_event(
                session,
                SessionEventKind.PLANNING_COMPLETED,
                payload={
                    "subtask_count": len(subtask_views),
                    "clarifying_question_count": len(
                        planner_output.clarifying_questions
                    ),
                },
            ),
        )

        pending_gate: PendingGateView | None = None
        if subtask_views:
            transition(
                session,
                SessionState.AWAITING_APPROVAL,
                allowed_from=(SessionState.PLANNING,),
            )
            await deps.sessions.publish(
                session.session_id,
                make_event(
                    session,
                    SessionEventKind.STATE_CHANGED,
                    payload={
                        "from": SessionState.PLANNING.value,
                        "to": session.state.value,
                    },
                ),
            )
            approval_gate = register_gate(
                session,
                prompt=(
                    f"Approve execution of {len(subtask_views)} subtask(s) "
                    f"derived from: {message!r}"
                ),
                subtasks=subtask_views,
            )
            pending_gate = approval_gate.to_view()
            await deps.sessions.publish(
                session.session_id,
                make_event(
                    session,
                    SessionEventKind.APPROVAL_REQUESTED,
                    payload={
                        "gate_id": approval_gate.gate_id,
                        "prompt": approval_gate.prompt,
                        "subtask_ids": tuple(st.id for st in approval_gate.subtasks),
                    },
                ),
            )
        else:
            # Pure clarifying questions — revert to the pre-planning state so the
            # user can refine the prompt without the session looking "running".
            session.state = previous_state
            await deps.sessions.publish(
                session.session_id,
                make_event(
                    session,
                    SessionEventKind.STATE_CHANGED,
                    payload={
                        "from": SessionState.PLANNING.value,
                        "to": session.state.value,
                    },
                ),
            )

        return ChatResponse(
            session_id=session.session_id,
            trace_id=session.trace_id,
            state=session.state,
            subtasks=subtask_views,
            clarifying_questions=tuple(planner_output.clarifying_questions),
            pending_gate=pending_gate,
        )


async def start_approval_resolution(
    deps: Dependencies,
    session: Session,
    gate_id: str,
    decision: ApprovalDecision,
    comments: str | None,
) -> ApprovalExecutionPlan:
    """Resolve an approval gate and perform the immediate state transition.

    The HTTP route uses this as the synchronous approval boundary: approve
    marks the gate resolved and moves the session to ``RUNNING``, then a
    background worker continues execution. Tests and direct callers can still
    use ``run_approval_resolution`` for the full approval-to-terminal flow.
    """

    with bind_trace(trace_id=session.trace_id, session_id=session.session_id):
        gate = resolve_gate(session, gate_id, decision, comments)
        await deps.sessions.publish(
            session.session_id,
            make_event(
                session,
                SessionEventKind.APPROVAL_RESOLVED,
                payload={
                    "gate_id": gate.gate_id,
                    "decision": decision.value,
                    "has_comments": comments is not None,
                },
            ),
        )

        if decision is ApprovalDecision.REJECTED:
            transition(
                session,
                SessionState.FAILED,
                allowed_from=(SessionState.AWAITING_APPROVAL,),
            )
            await deps.sessions.publish(
                session.session_id,
                make_event(
                    session,
                    SessionEventKind.STATE_CHANGED,
                    payload={
                        "from": SessionState.AWAITING_APPROVAL.value,
                        "to": SessionState.FAILED.value,
                    },
                ),
            )
            return ApprovalExecutionPlan(
                approved=False,
                approved_subtasks=(),
                gate_id=gate.gate_id,
                plan_snapshot_id=gate.plan_snapshot_id,
            )

        transition(
            session,
            SessionState.RUNNING,
            allowed_from=(SessionState.AWAITING_APPROVAL,),
        )
        await deps.sessions.publish(
            session.session_id,
            make_event(
                session,
                SessionEventKind.STATE_CHANGED,
                payload={
                    "from": SessionState.AWAITING_APPROVAL.value,
                    "to": SessionState.RUNNING.value,
                },
            ),
        )
        return ApprovalExecutionPlan(
            approved=True,
            approved_subtasks=gate.subtasks,
            gate_id=gate.gate_id,
            plan_snapshot_id=gate.plan_snapshot_id,
        )


async def continue_approval_execution(
    deps: Dependencies,
    session: Session,
    approved_subtasks: tuple[SubtaskView, ...],
    *,
    approval_gate_id: str | None = None,
    plan_snapshot_id: str | None = None,
    logger: logging.Logger | None = None,
) -> None:
    """Run the approved subtask snapshot and drive to a terminal state.

    Preconditions:
        The gate has already been resolved and ``session.state`` is ``RUNNING``.

    Subtask execution uses the approved gate snapshot to prevent the mutable
    ``session.subtasks`` view from influencing what is actually run.
    """
    log = logger or deps.logger
    with bind_trace(trace_id=session.trace_id, session_id=session.session_id):
        if session.state is not SessionState.RUNNING:
            raise SessionStateError(
                f"Session '{session.session_id}' cannot execute approved "
                f"subtasks while state is {session.state.value!r}."
            )

        # Phase 9B (manifest ADR): durable per-session/subtask artifact root.
        # The Phase 8A ``tempfile.mkdtemp`` pattern is retired — artifacts
        # now live under ``ARTIFACT_ROOT/<session>/<subtask>/`` so the
        # manifest, the REST download route, and any future replay tool
        # can find them by id rather than guessing a temp path.
        artifact_root = get_artifact_root()
        artifact_root.mkdir(parents=True, exist_ok=True)
        mechanical_pipeline = _mechanical_pipeline(deps, log)
        organic_pipeline = _organic_pipeline(deps, log)

        for index, subtask in enumerate(approved_subtasks):

            async def publish_progress(progress: PipelineProgress) -> None:
                await deps.sessions.publish(
                    session.session_id,
                    make_event(
                        session,
                        SessionEventKind.SUBTASK_PROGRESS,
                        payload=progress.to_payload(),
                    ),
                )

            await deps.sessions.publish(
                session.session_id,
                make_event(
                    session,
                    SessionEventKind.SUBTASK_STARTED,
                    payload={"subtask_id": subtask.id, "kind": subtask.kind},
                ),
            )

            subtask_sandbox = subtask_dir(artifact_root, session.session_id, subtask.id)
            try:
                if subtask.kind == "mechanical":
                    pipeline_payload = (
                        await mechanical_pipeline.execute(
                            session_id=session.session_id,
                            subtask=subtask,
                            output_dir=subtask_sandbox,
                            artifact_root=artifact_root,
                            trace_id=session.trace_id,
                            approval_gate_id=approval_gate_id,
                            plan_snapshot_id=plan_snapshot_id,
                            progress_callback=publish_progress,
                        )
                    ).to_event_payload()
                elif subtask.kind == "organic":
                    pipeline_payload = (
                        await organic_pipeline.execute(
                            session_id=session.session_id,
                            subtask=subtask,
                            output_dir=subtask_sandbox,
                            artifact_root=artifact_root,
                            trace_id=session.trace_id,
                            approval_gate_id=approval_gate_id,
                            plan_snapshot_id=plan_snapshot_id,
                            progress_callback=publish_progress,
                        )
                    ).to_event_payload()
                else:
                    raise NotImplementedError(
                        f"Subtask kind '{subtask.kind}' is not yet implemented."
                    )
            except Exception as exc:
                if isinstance(exc, ClarificationRequiredError):
                    resume_subtasks = (subtask, *approved_subtasks[index + 1 :])
                    gate = register_clarification(
                        session,
                        subtask=subtask,
                        resume_subtasks=resume_subtasks,
                        questions=exc.questions,
                        source_decision=exc.decision_metadata,
                        reason=exc.decision_reason,
                        plan_snapshot_id=plan_snapshot_id,
                        approval_gate_id=approval_gate_id,
                    )
                    transition(
                        session,
                        SessionState.AWAITING_USER_INPUT,
                        allowed_from=(SessionState.RUNNING,),
                    )
                    await deps.sessions.publish(
                        session.session_id,
                        make_event(
                            session,
                            SessionEventKind.STATE_CHANGED,
                            payload={
                                "from": SessionState.RUNNING.value,
                                "to": session.state.value,
                            },
                        ),
                    )
                    await deps.sessions.publish(
                        session.session_id,
                        make_event(
                            session,
                            SessionEventKind.CLARIFICATION_REQUESTED,
                            payload=gate.to_view(session.trace_id).model_dump(
                                mode="json"
                            ),
                        ),
                    )
                    return
                # fallback ADR item 6 (D5-b) — audit routing happens *inside* the
                # generic failure path, never with an early return of its own.
                #
                # Approved execution runs as an ``asyncio`` background task
                # whose done-callback only logs, so anything escaping this
                # handler would leave the session pinned at RUNNING forever
                # with no failure event. Routing is therefore best-effort: the
                # save is attempted first, its failure is recorded in the
                # manifest and the log, and control continues to the common
                # failure manifest -> SUBTASK_FAILED -> RUNNING->FAILED path in
                # every case. A lost audit write is bad; a zombie session that
                # never reports anything is worse.
                #
                # ``blocked_prohibited`` is deliberately not queued: fallback ADR
                # asks for *block* and *route to manual review* as different
                # outcomes, and the record schema refuses to model it anyway.
                prefilter_route_reason = getattr(exc, "prefilter_route_reason", None)
                prefilter_save_error: str | None = None
                if (
                    isinstance(exc, PrefilterBlockedError)
                    and exc.decision.decision == MANUAL_REVIEW_IP_DECISION
                ):
                    try:
                        deps.draft_queue.save_manual_review(
                            _manual_review_record(
                                deps=deps,
                                # The original four fields, unmodified: the
                                # rule table's ``reason_code`` reaches the
                                # durable record as-is and is never replaced by
                                # ``prefilter_route_reason``.
                                prefilter=exc.decision.model_dump(mode="json"),
                                originating_prompt=subtask.description,
                            )
                        )
                    except Exception as save_exc:
                        prefilter_save_error = f"{type(save_exc).__name__}: {save_exc}"
                        log.exception(
                            "prefilter_manual_review_save_failed",
                            extra={
                                "session_id": session.session_id,
                                "subtask_id": subtask.id,
                                "prefilter_route_reason": prefilter_route_reason,
                            },
                        )
                failure_stage = getattr(exc, "stage", subtask.kind)
                failure_metadata = {
                    "missing_features": tuple(getattr(exc, "missing_features", ())),
                    "violated_constraints": tuple(
                        getattr(exc, "violated_constraints", ())
                    ),
                    "retry_hint": getattr(exc, "retry_hint", None),
                    "decision_action": getattr(exc, "decision_action", None),
                    "decision_reason": getattr(exc, "decision_reason", None),
                    "decision_detail": getattr(exc, "decision_detail", None),
                    "decision": getattr(exc, "decision_metadata", {}),
                    "cad_coder_error_code": getattr(exc, "error_code", None),
                    "cad_coder_error_metadata": dict(getattr(exc, "metadata", {})),
                    "provider": getattr(exc, "provider", None),
                    "model": getattr(exc, "model", None),
                    "retry_count": getattr(exc, "retry_count", None),
                    "repair_attempts": getattr(exc, "repair_attempts", None),
                    "fallback_recommended": getattr(exc, "fallback_recommended", None),
                    "template_id": getattr(exc, "template_id", None),
                    "template_fallback_error_code": getattr(
                        exc, "template_fallback_error_code", None
                    ),
                    "template_fallback_error_metadata": dict(
                        getattr(exc, "template_fallback_error_metadata", {})
                    ),
                    # fallback ADR item 6: which pipeline blocked with which
                    # verdict, self-describing so a consumer never re-derives
                    # it from ``subtask.kind``. ``None`` for every other
                    # failure, like the sibling optional keys above.
                    "prefilter_route_reason": prefilter_route_reason,
                    # Honest record of a failed audit write (D5-b): the session
                    # still reaches FAILED, but the manifest says the
                    # manual-review request was not persisted.
                    "prefilter_manual_review_save_error": prefilter_save_error,
                }
                log.exception(
                    "subtask_failed",
                    extra={
                        "session_id": session.session_id,
                        "subtask_id": subtask.id,
                        "kind": subtask.kind,
                    },
                )
                # manifest ADR: emit a diagnostic failure manifest so UI /
                # replay / audit can see *why* this subtask failed
                # without grepping logs by trace_id.
                failure_manifest_path = write_failure_manifest(
                    session_id=session.session_id,
                    subtask_id=subtask.id,
                    stage=failure_stage,
                    error_type=type(exc).__name__,
                    detail=str(exc),
                    error_metadata=failure_metadata,
                    plan_snapshot_id=plan_snapshot_id,
                    approval_gate_id=approval_gate_id,
                    trace_id=session.trace_id,
                    root=artifact_root,
                )
                await deps.sessions.publish(
                    session.session_id,
                    make_event(
                        session,
                        SessionEventKind.SUBTASK_FAILED,
                        payload={
                            "subtask_id": subtask.id,
                            "kind": subtask.kind,
                            "stage": failure_stage,
                            "error_type": type(exc).__name__,
                            "detail": str(exc),
                            "missing_features": failure_metadata["missing_features"],
                            "violated_constraints": failure_metadata[
                                "violated_constraints"
                            ],
                            "retry_hint": failure_metadata["retry_hint"],
                            "decision_action": failure_metadata["decision_action"],
                            "decision_reason": failure_metadata["decision_reason"],
                            "decision_detail": failure_metadata["decision_detail"],
                            "cad_coder_error_code": failure_metadata[
                                "cad_coder_error_code"
                            ],
                            "cad_coder_error_metadata": failure_metadata[
                                "cad_coder_error_metadata"
                            ],
                            "provider": failure_metadata["provider"],
                            "model": failure_metadata["model"],
                            "retry_count": failure_metadata["retry_count"],
                            "repair_attempts": failure_metadata["repair_attempts"],
                            "fallback_recommended": failure_metadata[
                                "fallback_recommended"
                            ],
                            "template_id": failure_metadata["template_id"],
                            "template_fallback_error_code": failure_metadata[
                                "template_fallback_error_code"
                            ],
                            "template_fallback_error_metadata": failure_metadata[
                                "template_fallback_error_metadata"
                            ],
                            "prefilter_route_reason": failure_metadata[
                                "prefilter_route_reason"
                            ],
                            "prefilter_manual_review_save_error": failure_metadata[
                                "prefilter_manual_review_save_error"
                            ],
                            "manifest_path": str(failure_manifest_path),
                        },
                    ),
                )
                transition(
                    session,
                    SessionState.FAILED,
                    allowed_from=(SessionState.RUNNING,),
                )
                await deps.sessions.publish(
                    session.session_id,
                    make_event(
                        session,
                        SessionEventKind.STATE_CHANGED,
                        payload={
                            "from": SessionState.RUNNING.value,
                            "to": SessionState.FAILED.value,
                        },
                    ),
                )
                return

            await deps.sessions.publish(
                session.session_id,
                make_event(
                    session,
                    SessionEventKind.SUBTASK_COMPLETED,
                    payload={
                        "subtask_id": subtask.id,
                        "kind": subtask.kind,
                        **pipeline_payload,
                    },
                ),
            )

        transition(
            session,
            SessionState.COMPLETED,
            allowed_from=(SessionState.RUNNING,),
        )
        await deps.sessions.publish(
            session.session_id,
            make_event(
                session,
                SessionEventKind.STATE_CHANGED,
                payload={
                    "from": SessionState.RUNNING.value,
                    "to": SessionState.COMPLETED.value,
                },
            ),
        )
        log.info(
            "session_completed",
            extra={
                "session_id": session.session_id,
                "subtask_count": len(approved_subtasks),
                "artifact_root": str(artifact_root),
            },
        )


async def run_approval_resolution(
    deps: Dependencies,
    session: Session,
    gate_id: str,
    decision: ApprovalDecision,
    comments: str | None,
    *,
    logger: logging.Logger | None = None,
) -> None:
    """Resolve an approval gate and drive the session to its terminal state."""
    plan = await start_approval_resolution(deps, session, gate_id, decision, comments)
    if plan.should_execute:
        await continue_approval_execution(
            deps,
            session,
            plan.approved_subtasks,
            approval_gate_id=plan.gate_id,
            plan_snapshot_id=plan.plan_snapshot_id,
            logger=logger,
        )


async def start_clarification_resolution(
    deps: Dependencies,
    session: Session,
    clarification_id: str,
    answers: dict[str, ClarificationAnswerValue],
) -> ClarificationExecutionPlan:
    """Resolve a pending clarification and transition back to execution."""

    with bind_trace(trace_id=session.trace_id, session_id=session.session_id):
        if session.state is not SessionState.AWAITING_USER_INPUT:
            raise SessionStateError(
                f"Session '{session.session_id}' is {session.state.value}; "
                "/clarify is only allowed while awaiting user input."
            )
        gate = prepare_clarification_resolution(session, clarification_id, answers)
        if (
            deps.settings.capable_draft_route_enabled
            and _is_unknown_decorative_subject_gate(gate.source_decision)
        ):
            required_question_ids = tuple(
                question.id for question in gate.questions if question.required
            )
            answer_values = tuple(
                answers[question_id] for question_id in required_question_ids
            )
            if len(answer_values) != 1 or not isinstance(answer_values[0], str):
                raise SessionStateError(
                    "Decorative subject clarification requires one text answer."
                )
            subject = answer_values[0].strip()
            if not subject:
                raise SessionStateError(
                    "Decorative subject clarification answer must not be blank."
                )

            source_requirement_payload = gate.source_decision.get("requirement")
            if not isinstance(source_requirement_payload, dict):
                raise SessionStateError(
                    "Route clarification source is missing its requirement snapshot."
                )
            source_requirement = NewbieRequirementExtraction.model_validate(
                source_requirement_payload
            )
            route_result = (
                deps.natural_language_router.resolve_decorative_subject_clarification(
                    source_requirement,
                    subject,
                    capable_draft_route_enabled=(
                        deps.settings.capable_draft_route_enabled
                    ),
                )
            )
            next_revision = session.route_revision + 1
            route_view = _route_to_view(route_result, route_revision=next_revision)
            replacement_gate = (
                prepare_route_clarification(
                    session,
                    questions=_route_questions(route_result),
                    source_decision=_route_event_payload(route_view),
                    reason=route_result.reason.value,
                )
                if route_result.clarification_required
                else None
            )

            resolve_clarification(session, clarification_id, answers)
            session.route_revision = next_revision
            session.latest_route_result = route_view
            session.latest_intake_choice = None
            session.latest_intake_next_step = None
            if replacement_gate is None:
                transition(
                    session,
                    SessionState.CREATED,
                    allowed_from=(SessionState.AWAITING_USER_INPUT,),
                )
            else:
                commit_route_clarification(session, replacement_gate)

            await deps.sessions.publish(
                session.session_id,
                make_event(
                    session,
                    SessionEventKind.CLARIFICATION_RESOLVED,
                    payload={
                        "clarification_id": gate.clarification_id,
                        "subtask_id": gate.subtask_id,
                        "answered_question_ids": tuple(answers.keys()),
                        "requirement_snapshot_id": gate.requirement_snapshot_id,
                    },
                ),
            )
            await deps.sessions.publish(
                session.session_id,
                make_event(
                    session,
                    SessionEventKind.NATURAL_LANGUAGE_ROUTE_SELECTED,
                    payload=_route_event_payload(route_view),
                ),
            )
            if replacement_gate is None:
                await deps.sessions.publish(
                    session.session_id,
                    make_event(
                        session,
                        SessionEventKind.STATE_CHANGED,
                        payload={
                            "from": SessionState.AWAITING_USER_INPUT.value,
                            "to": session.state.value,
                        },
                    ),
                )
            else:
                await deps.sessions.publish(
                    session.session_id,
                    make_event(
                        session,
                        SessionEventKind.CLARIFICATION_REQUESTED,
                        payload=replacement_gate.to_view(session.trace_id).model_dump(
                            mode="json"
                        ),
                    ),
                )
            return ClarificationExecutionPlan(
                clarification_id=gate.clarification_id,
                resume_subtasks=(),
                approval_gate_id=gate.approval_gate_id,
                plan_snapshot_id=gate.plan_snapshot_id,
            )

        gate = resolve_clarification(session, clarification_id, answers)
        await deps.sessions.publish(
            session.session_id,
            make_event(
                session,
                SessionEventKind.CLARIFICATION_RESOLVED,
                payload={
                    "clarification_id": gate.clarification_id,
                    "subtask_id": gate.subtask_id,
                    "answered_question_ids": tuple(answers.keys()),
                    "requirement_snapshot_id": gate.requirement_snapshot_id,
                },
            ),
        )
        if gate.subtask_id is None:
            transition(
                session,
                SessionState.CREATED,
                allowed_from=(SessionState.AWAITING_USER_INPUT,),
            )
            await deps.sessions.publish(
                session.session_id,
                make_event(
                    session,
                    SessionEventKind.STATE_CHANGED,
                    payload={
                        "from": SessionState.AWAITING_USER_INPUT.value,
                        "to": session.state.value,
                    },
                ),
            )
            return ClarificationExecutionPlan(
                clarification_id=gate.clarification_id,
                resume_subtasks=(),
                approval_gate_id=gate.approval_gate_id,
                plan_snapshot_id=gate.plan_snapshot_id,
            )

        resume_subtasks = _merge_clarification_answers(
            gate.resume_subtasks,
            subtask_id=gate.subtask_id,
            questions=gate.questions,
            answers=answers,
        )
        session.subtasks = resume_subtasks
        transition(
            session,
            SessionState.RUNNING,
            allowed_from=(SessionState.AWAITING_USER_INPUT,),
        )
        await deps.sessions.publish(
            session.session_id,
            make_event(
                session,
                SessionEventKind.STATE_CHANGED,
                payload={
                    "from": SessionState.AWAITING_USER_INPUT.value,
                    "to": session.state.value,
                },
            ),
        )
        return ClarificationExecutionPlan(
            clarification_id=gate.clarification_id,
            resume_subtasks=resume_subtasks,
            approval_gate_id=gate.approval_gate_id,
            plan_snapshot_id=gate.plan_snapshot_id,
        )


async def run_clarification_resolution(
    deps: Dependencies,
    session: Session,
    clarification_id: str,
    answers: dict[str, ClarificationAnswerValue],
    *,
    logger: logging.Logger | None = None,
) -> ClarifyResponse:
    plan = await start_clarification_resolution(
        deps,
        session,
        clarification_id,
        answers,
    )
    if plan.should_execute:
        await continue_approval_execution(
            deps,
            session,
            plan.resume_subtasks,
            approval_gate_id=plan.approval_gate_id,
            plan_snapshot_id=plan.plan_snapshot_id,
            logger=logger,
        )
    return ClarifyResponse(
        session_id=session.session_id,
        clarification_id=clarification_id,
        accepted=True,
        state=session.state,
        route_result=session.latest_route_result,
        pending_clarification=pending_clarification_view(session),
    )


async def record_intake_choice(
    deps: Dependencies,
    session: Session,
    request: IntakeChoiceRequest,
) -> IntakeChoiceResponse:
    """Record a Phase 12V user choice for the latest asset intake route.

    This function intentionally does not start CAD/STL/Orca execution and does
    not mutate the runtime asset catalog. It only records the user's selected
    boundary so later phases can safely branch from an auditable decision.
    """

    with bind_trace(trace_id=session.trace_id, session_id=session.session_id):
        if request.session_id != session.session_id:
            raise SessionStateError("Intake choice session id does not match.")
        if session.state not in (
            SessionState.CREATED,
            SessionState.AWAITING_USER_INPUT,
        ):
            raise SessionStateError(
                f"Session '{session.session_id}' is {session.state.value}; "
                "intake choices are only allowed before runtime execution."
            )
        route = session.latest_route_result
        if route is None:
            raise SessionStateError(
                f"Session '{session.session_id}' has no route result to choose from."
            )
        if request.route_snapshot_id != route.route_snapshot_id:
            raise SessionStateError(
                "Intake choice was made against a stale route snapshot."
            )
        if session.latest_intake_choice is not None:
            raise SessionStateError(
                "Latest intake route already has a recorded choice."
            )
        allowed_action = _allowed_intake_action(route)
        if request.action is not allowed_action:
            raise SessionStateError(
                f"Action {request.action.value!r} is not valid for latest "
                f"intake route; expected {allowed_action.value!r}."
            )
        if request.action is IntakeChoiceAction.CREATE_NEW_DRAFT:
            if (
                route.intake_decision is None
                or not route.intake_decision.new_draft_allowed
            ):
                raise SessionStateError(
                    "New draft creation is only allowed for new_draft_allowed intake."
                )

        response = _intake_choice_response(session, route, request.action)
        session.latest_intake_choice = response
        await deps.sessions.publish(
            session.session_id,
            make_event(
                session,
                SessionEventKind.INTAKE_CHOICE_RECORDED,
                payload=response.model_dump(mode="json"),
            ),
        )
        return response


async def record_intake_next_step(
    deps: Dependencies,
    session: Session,
    request: IntakeNextStepRequest,
) -> IntakeNextStepView:
    """Open the next safe boundary from a previously recorded intake choice.

    This bridge is intentionally narrower than the later product runtime: only
    ``create_new_draft`` may call the draft authoring pipeline. Other actions
    are recorded as review/selection boundaries and do not start CAD/STL/Orca.
    """

    with bind_trace(trace_id=session.trace_id, session_id=session.session_id):
        if request.session_id != session.session_id:
            raise SessionStateError("Intake next-step session id does not match.")
        if session.state not in (
            SessionState.CREATED,
            SessionState.AWAITING_USER_INPUT,
        ):
            raise SessionStateError(
                f"Session '{session.session_id}' is {session.state.value}; "
                "intake next steps are only allowed before runtime execution."
            )
        route = session.latest_route_result
        choice = session.latest_intake_choice
        if route is None or choice is None:
            raise SessionStateError(
                f"Session '{session.session_id}' has no recorded intake choice."
            )
        if request.route_snapshot_id != route.route_snapshot_id:
            raise SessionStateError(
                "Intake next step was requested against a stale route snapshot."
            )
        if choice.route_snapshot_id != route.route_snapshot_id:
            raise SessionStateError(
                "Recorded intake choice no longer matches the latest route snapshot."
            )
        if session.latest_intake_next_step is not None:
            raise SessionStateError(
                "Latest intake choice already has a recorded next-step boundary."
            )

        status = _INTAKE_NEXT_STEP_STATUS[choice.action]
        if choice.action is IntakeChoiceAction.CREATE_NEW_DRAFT:
            if (
                route.intake_decision is None
                or not route.intake_decision.new_draft_allowed
            ):
                raise SessionStateError(
                    "Draft authoring is only allowed for new_draft_allowed intake."
                )
            authoring_request = _authoring_request_from_route(route)
            authoring_result = await deps.asset_authoring.handle_with_capable_fallback(
                authoring_request
            )
            if authoring_result.status is DraftAssetAuthoringStatus.DRAFT_CREATED:
                if authoring_result.draft_asset is None:
                    raise SessionStateError("Draft authoring returned no draft asset.")
                draft = authoring_result.draft_asset
                if (
                    draft.release_allowed
                    or draft.runtime_catalog_registered
                    or draft.visual_quality_status.value != "review_required"
                    or draft.legal_review_status.value != "not_reviewed"
                ):
                    raise SessionStateError(
                        "Draft authoring returned a draft that is not review-closed."
                    )
                # fallback ADR Item 5 (persistence slice): the draft must outlive the
                # session, otherwise "draft-only + human review" has nothing for a
                # human to review later. Every draft of this branch is persisted —
                # capable-model generated and deterministic cat-keyring alike — so
                # the queue never disagrees with what the caller was told.
                #
                # A storage failure is a server defect, not a client state
                # conflict: it deliberately propagates (HTTP 500) instead of being
                # translated into SessionStateError (HTTP 409) or swallowed into a
                # `draft_created` response that lies about what was written (R10).
                # Because the boundary state and the success event are only
                # committed after this call, a failure leaves neither behind.
                deps.draft_queue.save_draft(draft)
                response = _closed_next_step(
                    session=session,
                    route=route,
                    choice=choice,
                    status=status,
                    draft_asset=draft.model_dump(mode="json"),
                    draft_authoring_status=authoring_result.status.value,
                    draft_authoring_reason=authoring_result.reason,
                )
            elif authoring_result.status is DraftAssetAuthoringStatus.ASK_USER:
                # Capable-model fallback declined (generator absent, recoverable
                # generation failure, sandbox violation, or an IP/abuse pre-filter
                # block raised before the model ran): stay on a closed
                # information-needed boundary and preserve the structured reason
                # rather than collapsing into a SessionStateError.
                #
                # The structured verdict is lifted once and reused, so what is
                # persisted and what the caller is told cannot disagree. The
                # fail-closed contract inside `_prefilter_from_authoring`
                # (blocked reason without a structured dict -> SessionStateError)
                # is unchanged.
                prefilter = _prefilter_from_authoring(authoring_result)
                if (
                    prefilter is not None
                    and prefilter.get("decision") == MANUAL_REVIEW_IP_DECISION
                ):
                    # fallback ADR Item 6: a manual-review verdict that only shows up
                    # in the response is not "routed to manual review" — nobody
                    # can look at it later. Persist it before the boundary and
                    # the success event are committed, with the same failure
                    # contract as draft persistence: a storage fault propagates
                    # (HTTP 500), it is not translated into SessionStateError
                    # (409) nor swallowed into a response that claims a review
                    # request exists. On failure neither the boundary nor the
                    # event is left behind (atomicity).
                    #
                    # Only this verdict is routed. fallback ADR asks for *block* and
                    # *route to manual review* as different outcomes, and a
                    # prohibited category sitting in a review queue would read as
                    # something a reviewer may approve — that case stops at the
                    # structured blocked response.
                    deps.draft_queue.save_manual_review(
                        _manual_review_record(
                            deps=deps,
                            prefilter=prefilter,
                            originating_prompt=authoring_request.user_prompt_ko,
                        )
                    )
                response = _closed_next_step(
                    session=session,
                    route=route,
                    choice=choice,
                    status=IntakeNextStepStatus.INFORMATION_NEEDED,
                    draft_authoring_status=authoring_result.status.value,
                    draft_authoring_reason=authoring_result.reason,
                    prefilter=prefilter,
                )
            else:
                raise SessionStateError(
                    "Draft authoring did not create a new draft; re-run asset intake."
                )
        else:
            response = _closed_next_step(
                session=session,
                route=route,
                choice=choice,
                status=status,
            )

        session.latest_intake_next_step = response
        await deps.sessions.publish(
            session.session_id,
            make_event(
                session,
                SessionEventKind.INTAKE_NEXT_STEP_RECORDED,
                payload=response.model_dump(mode="json"),
            ),
        )
        return response


__all__ = [
    "PLANNER_PREFILTER_ROUTE_REASONS",
    "ApprovalExecutionPlan",
    "ClarificationExecutionPlan",
    "continue_approval_execution",
    "record_intake_choice",
    "record_intake_next_step",
    "run_clarification_resolution",
    "run_approval_resolution",
    "run_chat_turn",
    "start_clarification_resolution",
    "start_approval_resolution",
]
