"""Subtask execution services used by the orchestrator graph.

Graph nodes own state transitions and event publication. Concrete
domain work lives here so Phase 8B can add real CAD/Orca/profile logic
without turning ``graph.py`` into the pipeline implementation.

Phase 9B note (manifest ADR):
``MechanicalPipeline.execute`` now also produces a
:class:`~modules.artifacts.SubtaskArtifactManifest` and writes it under
the per-subtask sandbox the orchestrator passes in. The flat-key
``PipelineArtifacts`` shim stays in place until Phase 9D close so the
WS event payload contract does not break in flight.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from types import MappingProxyType

from apps.orchestrator.exceptions import (
    ClarificationRequiredError,
    OrchestratorConfigError,
    PrefilterBlockedError,
)
from apps.orchestrator.provider_switch import (
    CADCoderProviderFactory,
    CADCoderProviderRequest,
    CADCoderProviderSwitchError,
)
from apps.orchestrator.schemas import ClarificationQuestion, SubtaskView
from modules.agents.cad_coder.base import BaseCADCoderAgent
from modules.agents.cad_coder.exceptions import CADCoderAgentError
from modules.agents.cad_coder.schemas import CADCoderRequest, CADCoderResponse, CADDsl
from modules.agents.self_healer.base import BaseSelfHealerAgent
from modules.agents.self_healer.schemas import SelfHealerRequest
from modules.artifacts import (
    ArtifactKind,
    SubtaskArtifactManifest,
    build_artifact_ref,
    write_manifest,
)
from modules.cad_mechanical.base import BaseMechanicalCADGenerator
from modules.cad_mechanical.schemas import CADDialect, GenerationRequest
from modules.newbie_request.visual_quality import visual_quality_metadata
from modules.orchestrator_decision.base import BaseDecisionEngine
from modules.orchestrator_decision.exceptions import OrchestratorDecisionError
from modules.orchestrator_decision.schemas import (
    DecisionAction,
    DecisionRequest,
    DecisionResult,
    ProviderPolicy,
)
from modules.organic_generator.base import BaseOrganicGenerator
from modules.organic_generator.schemas import (
    GenerationRequest as OrganicGenerationRequest,
)
from modules.requirements.base import BaseRequirementExtractor
from modules.requirements.schemas import RequirementExtractionRequest, RequirementSpec
from modules.semantic_validator.base import BaseSemanticValidator
from modules.semantic_validator.exceptions import SemanticValidationPipelineError
from modules.semantic_validator.schemas import (
    SemanticValidationReport,
    SemanticValidationRequest,
)
from modules.slicer.base import BaseSlicer
from modules.slicer.schemas import MaterialPreset, PrinterProfile, SliceRequest
from modules.template.fallback import (
    TemplateFallbackContext,
    TemplateFallbackRender,
    TemplateFallbackRouter,
)
from modules.template.ip_abuse_prefilter import FallbackGateDecision
from modules.validator.base import BaseMeshValidator
from modules.validator.schemas import ValidationCheck, ValidationRequest

#: Pre-filter predicate contract: prompt in, structured verdict out, no side
#: effects. Satisfied by
#: ``modules.template.ip_abuse_prefilter.should_invoke_fallback_agent``; tests
#: substitute a stub with the same shape.
PipelinePrefilterGate = Callable[[str], FallbackGateDecision]

#: Machine-readable ``prefilter_route_reason`` per (pipeline, blocked verdict),
#: fallback ADR item 6. Mirrors ``CAPABLE_PREFILTER_BLOCKED_REASONS``: the mapping
#: is closed and interpolates neither the variable ``reason_code`` nor any
#: human-facing text, so consumers branch on an exact string.
#:
#: This is deliberately NOT ``FallbackGateDecision.reason_code``. That field is
#: owned by the rule tables (``prohibited_category`` /
#: ``trademark_or_ip_review_required``) and is preserved verbatim into the
#: durable manual-review record; overwriting it with a route reason would
#: forge the audit trail. The route reason is carried alongside it so a failure
#: payload says which pipeline blocked without the consumer re-deriving it from
#: ``subtask.kind``.
MECHANICAL_PREFILTER_ROUTE_REASONS: Mapping[str, str] = MappingProxyType(
    {
        "blocked_prohibited": "mechanical_prefilter_blocked_prohibited",
        "manual_review_ip": "mechanical_prefilter_manual_review_ip",
    }
)
ORGANIC_PREFILTER_ROUTE_REASONS: Mapping[str, str] = MappingProxyType(
    {
        "blocked_prohibited": "organic_prefilter_blocked_prohibited",
        "manual_review_ip": "organic_prefilter_manual_review_ip",
    }
)


def _enforce_prefilter(
    prefilter: PipelinePrefilterGate,
    *,
    prompt: str,
    route_reasons: Mapping[str, str],
) -> None:
    """Run the IP/abuse guard before anything else in a pipeline.

    One helper shared by both pipelines on purpose (nothing pipeline-specific
    but the route-reason table): two copies would drift and only one of them
    would get fixed.

    An unmapped non-``allow`` verdict fails closed — generation must not
    proceed just because the decision taxonomy grew a value this wiring has
    never seen.
    """
    decision = prefilter(prompt)
    if decision.decision == "allow":
        return
    route_reason = route_reasons.get(decision.decision)
    if route_reason is None:
        raise OrchestratorConfigError(
            f"Unmapped IP/abuse pre-filter verdict {decision.decision!r} in "
            "pipeline wiring; refusing to generate on an unknown safety verdict."
        )
    # D7: the user-facing text is the rule table's, never invented here.
    raise PrefilterBlockedError(
        decision.user_message_ko,
        decision=decision,
        prefilter_route_reason=route_reason,
    )


_DSL_TO_DIALECT: dict[CADDsl, CADDialect] = {
    "cadquery": CADDialect.CADQUERY,
    "build123d": CADDialect.BUILD123D,
    "openscad": CADDialect.OPENSCAD,
}


def _generation_mode_for_dsl(dsl: CADDsl) -> str:
    if dsl == "openscad":
        return "freeform_openscad"
    return dsl


def _semantic_error(
    subtask_id: str,
    report: SemanticValidationReport,
    decision_result: DecisionResult | None = None,
) -> SemanticValidationPipelineError:
    parts = [f"Semantic validation failed for subtask '{subtask_id}'."]
    if report.missing_features:
        parts.append("Missing: " + ", ".join(report.missing_features) + ".")
    if report.violated_constraints:
        parts.append("Violations: " + ", ".join(report.violated_constraints) + ".")
    if report.retry_hint:
        parts.append(report.retry_hint)
    return SemanticValidationPipelineError(
        " ".join(parts),
        missing_features=report.missing_features,
        violated_constraints=report.violated_constraints,
        retry_hint=report.retry_hint,
        decision_metadata=decision_result.model_dump(mode="json")
        if decision_result is not None
        else None,
    )


def _decision_progress_detail(result: DecisionResult) -> dict[str, object]:
    return {
        "decision_action": result.action.value,
        "decision_reason": result.reason.value,
        "decision_detail": result.detail,
        "should_continue": result.should_continue,
        "target_provider": result.target_provider,
        "target_template_id": result.target_template_id,
        "questions": result.questions,
        "next_attempt": result.next_attempt,
        "metadata": result.metadata,
    }


def _cad_coder_provider_name(cad_coder: BaseCADCoderAgent) -> str:
    provider_name = getattr(cad_coder, "provider_name", None)
    if isinstance(provider_name, str) and provider_name.strip():
        return provider_name
    return cad_coder.adapter_name


def _cad_error_metadata(exc: CADCoderAgentError) -> dict[str, object]:
    return {
        "cad_coder_error_code": exc.error_code,
        "cad_coder_error_metadata": dict(exc.metadata),
        "provider": exc.provider,
        "model": exc.model,
        "retry_count": exc.retry_count,
        "repair_attempts": exc.repair_attempts,
        "fallback_recommended": exc.fallback_recommended,
    }


def _source_cad_error_metadata(exc: CADCoderAgentError) -> dict[str, object]:
    return {
        "source_cad_coder_error_code": exc.error_code,
        "source_cad_coder_error_metadata": dict(exc.metadata),
        "source_provider": exc.provider,
        "source_model": exc.model,
        "source_retry_count": exc.retry_count,
        "source_repair_attempts": exc.repair_attempts,
        "source_fallback_recommended": exc.fallback_recommended,
    }


def _metadata_float(metadata: dict[str, object], key: str) -> float | None:
    value = metadata.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _template_attempt_metadata(
    *,
    rendered: TemplateFallbackRender,
    fallback_reason: str | None,
    source_provider: str | None,
    trace_id: str | None,
    cad_error: CADCoderAgentError | None = None,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "original_attempt_source": "local_freeform",
        "fallback_source": "template",
        "fallback_reason": fallback_reason,
        "recipe_id": rendered.recipe_id,
        "template_id": rendered.recipe_id,
        "recipe_version": rendered.recipe_version,
        "template_params": rendered.parameters,
        "template_parameters": rendered.parameters,
        "param_hash": rendered.param_hash,
        "rendered_code_hash": rendered.code_hash,
        "source_provider": source_provider,
        "trace_id": trace_id,
    }
    if cad_error is not None:
        metadata.update(_source_cad_error_metadata(cad_error))
    return metadata


def _selected_template_fallback(
    attempt_history: tuple[dict[str, object], ...],
) -> dict[str, object] | None:
    for attempt in reversed(attempt_history):
        if attempt.get("attempt_kind") == "template_fallback":
            return attempt
    return None


def _visual_quality_metadata(
    requirements: RequirementSpec | None,
) -> dict[str, object]:
    if requirements is None:
        return visual_quality_metadata(required=False, reason="no_requirements")

    decorative_tags = {"decorative", "bear", "flat_2.5d"}
    decorative_features = {
        "bear_head",
        "ears",
        "eyes",
        "nose_or_muzzle",
        "raised_logo_or_text",
    }
    required = (
        requirements.object_type == "keyring"
        or bool(set(requirements.intent_tags) & decorative_tags)
        or bool(set(requirements.required_features) & decorative_features)
    )
    return visual_quality_metadata(
        required=required,
        required_features=requirements.required_features,
    )


def _clarification_questions_from_decision(
    result: DecisionResult,
) -> tuple[ClarificationQuestion, ...]:
    raw_questions = tuple(result.questions[:3]) or (
        "제작에 필요한 크기와 주요 기능을 알려주세요.",
    )
    questions: list[ClarificationQuestion] = []
    for index, question in enumerate(raw_questions, start=1):
        questions.append(
            ClarificationQuestion(
                id=f"q{index}",
                field=f"clarification_{index}",
                question=question,
                required=True,
                unit=None,
            )
        )
    return tuple(questions)


@dataclass(frozen=True)
class PipelineProgress:
    """Human-visible progress point emitted while a subtask is running."""

    subtask_id: str
    kind: str
    stage: str
    status: str
    message: str
    detail: dict[str, object] | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "subtask_id": self.subtask_id,
            "kind": self.kind,
            "stage": self.stage,
            "status": self.status,
            "message": self.message,
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload


PipelineProgressCallback = Callable[[PipelineProgress], Awaitable[None]]


@dataclass(frozen=True)
class PipelineArtifacts:
    """Artifact references emitted when a subtask completes.

    Phase 9D close: legacy flat ``stl_path`` / ``gcode_path`` / ``threemf_path``
    fields and event-payload keys are removed. UI/API consumers MUST read
    artifact paths exclusively from ``manifest.artifacts[].relative_uri``
    (manifest ADR invariant #5).
    """

    adapter_cad: str
    adapter_slicer: str
    manifest: SubtaskArtifactManifest

    def to_event_payload(self) -> dict[str, object]:
        return {
            "adapter_cad": self.adapter_cad,
            "adapter_slicer": self.adapter_slicer,
            "manifest": self.manifest.model_dump(mode="json"),
        }


@dataclass(frozen=True)
class MechanicalPipeline:
    """Mechanical CAD -> validation -> slicing pipeline.

    Phase 8A deliberately runs this only against mock adapters. Real
    adapter wiring should keep the same orchestration-facing method and
    move profile/artifact-store details into this service.
    """

    cad_coder: BaseCADCoderAgent
    requirement_extractor: BaseRequirementExtractor
    semantic_validator: BaseSemanticValidator
    decision_engine: BaseDecisionEngine
    cad_coder_provider_factory: CADCoderProviderFactory
    self_healer: BaseSelfHealerAgent
    cad: BaseMechanicalCADGenerator
    validator: BaseMeshValidator
    slicer: BaseSlicer
    logger: logging.Logger
    printer: PrinterProfile
    material: MaterialPreset
    # fallback ADR item 6: mandatory, no default. A pipeline that can be assembled
    # without an IP/abuse gate is exactly the defect this ADR is about, so every
    # construction site is forced to name a gate instead of inheriting one.
    prefilter: PipelinePrefilterGate
    process_profile: str | None = None
    template_fallback_router: TemplateFallbackRouter | None = None
    # Phase 10A.1: the cad_coder LLM must be told which DSL to emit.
    # Defaults to "cadquery" to preserve historical behavior.  The
    # orchestrator wiring layer derives this from CAD_CODER_DSL (priority)
    # or CAD_MECHANICAL_ADAPTER so the cad generator and the coder agree.
    cad_coder_dsl: CADDsl = "cadquery"
    semantic_max_attempts: int = 2
    product_mode: bool = False
    decision_cost_budget_usd: float | None = None
    decision_timeout_budget_ms: int | None = None
    decision_provider_policy: ProviderPolicy = ProviderPolicy.FALLBACK_ALLOWED
    decision_plan_tier: str | None = None
    decision_fallback_provider: str | None = None
    decision_fallback_model: str | None = None
    decision_fallback_provider_available: bool = True
    decision_quota_remaining: float | None = None
    decision_max_provider_fallbacks: int = 1

    async def execute(
        self,
        *,
        session_id: str,
        subtask: SubtaskView,
        output_dir: Path,
        artifact_root: Path,
        trace_id: str | None = None,
        approval_gate_id: str | None = None,
        plan_snapshot_id: str | None = None,
        progress_callback: PipelineProgressCallback | None = None,
    ) -> PipelineArtifacts:
        # fallback ADR item 6: FIRST statement — ahead of the requirement extractor
        # (the first LLM call) and ahead of every progress event. "Do not
        # generate then catch."
        _enforce_prefilter(
            self.prefilter,
            prompt=subtask.description,
            route_reasons=MECHANICAL_PREFILTER_ROUTE_REASONS,
        )
        pipeline_started = perf_counter()
        requirements = await self._extract_requirements(
            subtask=subtask,
            trace_id=trace_id,
            progress_callback=progress_callback,
        )
        active_cad_coder = self.cad_coder
        if requirements is not None and requirements.needs_clarification:
            provider = _cad_coder_provider_name(active_cad_coder)
            pre_decision_result = self.decision_engine.decide(
                DecisionRequest(
                    session_id=session_id,
                    subtask_id=subtask.id,
                    user_prompt=subtask.description,
                    requirements=requirements,
                    generation_mode=_generation_mode_for_dsl(self.cad_coder_dsl),
                    provider=provider,
                    semantic_report=None,
                    attempt_index=0,
                    max_attempts=max(1, self.semantic_max_attempts),
                    cost_budget_usd=self.decision_cost_budget_usd,
                    elapsed_ms=int((perf_counter() - pipeline_started) * 1000),
                    timeout_budget_ms=self.decision_timeout_budget_ms,
                    approval_snapshot_matches=True,
                    product_mode=self.product_mode,
                    fallback_provider=self.decision_fallback_provider,
                    fallback_model=self.decision_fallback_model,
                    fallback_provider_available=(
                        self.decision_fallback_provider_available
                    ),
                    provider_policy=self.decision_provider_policy,
                    plan_tier=self.decision_plan_tier,
                    quota_remaining=self.decision_quota_remaining,
                    semantic_failure_count=0,
                    max_provider_fallbacks=self.decision_max_provider_fallbacks,
                    trace_id=trace_id,
                    metadata={
                        "cad_coder_adapter": active_cad_coder.adapter_name,
                        "cad_coder_provider": provider,
                        "cad_coder_dsl": self.cad_coder_dsl,
                    },
                )
            )
            await self._emit_progress(
                progress_callback,
                subtask=subtask,
                stage="clarification",
                status="blocked",
                message="모델 생성을 시작하기 전에 추가 정보가 필요합니다.",
                detail=_decision_progress_detail(pre_decision_result),
            )
            if pre_decision_result.action == DecisionAction.ASK_USER_CLARIFICATION:
                raise ClarificationRequiredError(
                    "Additional user clarification is required before CAD generation.",
                    decision_result=pre_decision_result,
                    questions=_clarification_questions_from_decision(
                        pre_decision_result
                    ),
                )
            if pre_decision_result.action != DecisionAction.PROCEED_TO_SLICE:
                raise OrchestratorDecisionError(
                    "Pre-generation clarification decision is not executable: "
                    f"{pre_decision_result.action.value}.",
                    decision_result=pre_decision_result,
                )
        attempt = 1
        max_attempts = max(1, self.semantic_max_attempts)
        current_description = subtask.description
        semantic_report: SemanticValidationReport | None = None
        decision_result: DecisionResult | None = None
        attempt_kind = "initial"
        source_provider: str | None = None
        provider_fallbacks_used = 0
        template_fallbacks_used = 0
        pending_template_fallback: TemplateFallbackRender | None = None
        pending_template_reason: str | None = None
        pending_template_error: CADCoderAgentError | None = None
        attempt_history: list[dict[str, object]] = []
        while True:
            active_provider = (
                "template"
                if pending_template_fallback is not None
                else _cad_coder_provider_name(active_cad_coder)
            )
            active_adapter = (
                f"template:{pending_template_fallback.recipe_id}"
                if pending_template_fallback is not None
                else active_cad_coder.adapter_name
            )
            attempt_metadata: dict[str, object] = {
                "attempt_index": attempt - 1,
                "attempt_kind": attempt_kind,
                "provider": active_provider,
                "cad_coder_adapter": active_adapter,
                "model": self.decision_fallback_model
                if attempt_kind == "provider_fallback"
                else None,
                "source_provider": source_provider,
                "trace_id": trace_id,
            }
            if pending_template_fallback is not None:
                attempt_metadata.update(
                    _template_attempt_metadata(
                        rendered=pending_template_fallback,
                        fallback_reason=pending_template_reason,
                        source_provider=source_provider,
                        trace_id=trace_id,
                        cad_error=pending_template_error,
                    )
                )
            attempt_output_dir = output_dir / "attempts" / f"attempt-{attempt - 1}"
            attempt_output_dir.mkdir(parents=True, exist_ok=True)
            attempt_metadata["artifact_dir"] = str(attempt_output_dir)

            if pending_template_fallback is not None:
                rendered_template = pending_template_fallback
                await self._emit_progress(
                    progress_callback,
                    subtask=subtask,
                    stage="template_fallback",
                    status="started",
                    message=(
                        "로컬 모델이 CAD 코드 생성 규칙을 반복해서 지키지 못해 "
                        "안전한 템플릿 경로로 전환했습니다."
                    ),
                    detail=attempt_metadata,
                )
                coder_response = CADCoderResponse(
                    subtask_id=subtask.id,
                    dsl="openscad",
                    code=rendered_template.code,
                    entrypoint="main",
                    trace_id=trace_id,
                )
                await self._emit_progress(
                    progress_callback,
                    subtask=subtask,
                    stage="template_fallback",
                    status="completed",
                    message="템플릿 기반 CAD 명령 렌더링이 완료되었습니다.",
                    detail=attempt_metadata,
                )
                pending_template_fallback = None
                pending_template_reason = None
                pending_template_error = None
            else:
                await self._emit_progress(
                    progress_callback,
                    subtask=subtask,
                    stage="cad_code",
                    status="started",
                    message="CAD 명령을 생성하는 중입니다.",
                    detail={
                        "dsl": self.cad_coder_dsl,
                        "provider": active_provider,
                        "adapter": active_adapter,
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        **attempt_metadata,
                    },
                )
                try:
                    coder_response = await active_cad_coder.generate_code(
                        CADCoderRequest(
                            subtask_id=subtask.id,
                            description=current_description,
                            trace_id=trace_id,
                            dsl=self.cad_coder_dsl,
                            requirements=requirements,
                        )
                    )
                except CADCoderAgentError as exc:
                    attempt_history.append(
                        {
                            **attempt_metadata,
                            "cad_contract_passed": False,
                            "status": "failed",
                            **_cad_error_metadata(exc),
                        }
                    )
                    await self._emit_progress(
                        progress_callback,
                        subtask=subtask,
                        stage="cad_code",
                        status="failed",
                        message="CAD 코드 생성 계약을 만족하지 못했습니다.",
                        detail={**attempt_metadata, **_cad_error_metadata(exc)},
                    )
                    if (
                        template_fallbacks_used >= 1
                        or exc.fallback_recommended != "template"
                    ):
                        raise
                    rendered_fallback = self._render_template_fallback(
                        description=subtask.description,
                        requirements=requirements,
                        fallback_reason=exc.error_code,
                        provider=exc.provider or active_provider,
                        model=exc.model,
                        repair_attempts=exc.repair_attempts,
                        cad_error=exc,
                    )
                    if rendered_fallback is None:
                        raise
                    template_fallbacks_used += 1
                    source_provider = active_provider
                    attempt_kind = "template_fallback"
                    pending_template_fallback = rendered_fallback
                    pending_template_reason = exc.error_code
                    pending_template_error = exc
                    attempt += 1
                    continue

            self.logger.info(
                "subtask_cad_code_generated",
                extra={
                    "session_id": session_id,
                    "subtask_id": subtask.id,
                    "dsl": coder_response.dsl,
                    "provider": active_provider,
                    "adapter": active_adapter,
                    "attempt": attempt,
                    "attempt_kind": attempt_kind,
                    "source_provider": source_provider,
                },
            )
            await self._emit_progress(
                progress_callback,
                subtask=subtask,
                stage="cad_code",
                status="completed",
                message="CAD 명령 생성이 완료되었습니다.",
                detail={
                    "dsl": coder_response.dsl,
                    "adapter": active_adapter,
                    "attempt": attempt,
                    **attempt_metadata,
                },
            )
            await self._emit_progress(
                progress_callback,
                subtask=subtask,
                stage="cad_generate",
                status="started",
                message="CAD 런타임에 모델 생성 명령을 전송했습니다.",
                detail={"dialect": coder_response.dsl, "output_format": "stl"},
            )
            cad_result = await self.cad.generate(
                GenerationRequest(
                    prompt=current_description,
                    code=coder_response.code,
                    dialect=_DSL_TO_DIALECT[coder_response.dsl],
                    session_id=session_id,
                    output_dir=attempt_output_dir,
                    format="stl",
                )
            )
            self.logger.info(
                "subtask_cad_completed",
                extra={
                    "session_id": session_id,
                    "subtask_id": subtask.id,
                    "stl_path": str(cad_result.path),
                    "adapter": cad_result.adapter_used,
                    "cad_coder_provider": active_provider,
                    "attempt": attempt,
                    "attempt_kind": attempt_kind,
                    "source_provider": source_provider,
                },
            )
            await self._emit_progress(
                progress_callback,
                subtask=subtask,
                stage="cad_generate",
                status="completed",
                message="STL 메시 생성이 완료되었습니다.",
                detail={
                    "adapter": cad_result.adapter_used,
                    "stl_path": str(cad_result.path),
                    "cad_runtime": cad_result.metadata,
                    "attempt": attempt,
                    **attempt_metadata,
                },
            )

            semantic_report = await self._validate_semantics(
                subtask=subtask,
                code=coder_response.code,
                cad_path=cad_result.path,
                requirements=requirements,
                trace_id=trace_id,
                progress_callback=progress_callback,
            )
            decision_result = self.decision_engine.decide(
                DecisionRequest(
                    session_id=session_id,
                    subtask_id=subtask.id,
                    user_prompt=subtask.description,
                    requirements=requirements,
                    generation_mode=_generation_mode_for_dsl(coder_response.dsl),
                    provider=active_provider,
                    semantic_report=semantic_report,
                    attempt_index=attempt - 1,
                    max_attempts=max_attempts,
                    cost_budget_usd=self.decision_cost_budget_usd,
                    elapsed_ms=int((perf_counter() - pipeline_started) * 1000),
                    timeout_budget_ms=self.decision_timeout_budget_ms,
                    approval_snapshot_matches=True,
                    product_mode=self.product_mode,
                    fallback_provider=self.decision_fallback_provider,
                    fallback_model=self.decision_fallback_model,
                    fallback_provider_available=self.decision_fallback_provider_available,
                    provider_policy=self.decision_provider_policy,
                    plan_tier=self.decision_plan_tier,
                    quota_remaining=self.decision_quota_remaining,
                    semantic_failure_count=attempt - 1,
                    matching_template_id=self._matching_template_id(
                        description=subtask.description,
                        requirements=requirements,
                    )
                    if attempt_kind != "template_fallback"
                    else None,
                    max_provider_fallbacks=(
                        self.decision_max_provider_fallbacks
                        - provider_fallbacks_used
                    ),
                    trace_id=trace_id,
                    metadata={
                        "cad_adapter": cad_result.adapter_used,
                        "cad_coder_adapter": active_adapter,
                        "cad_coder_provider": active_provider,
                        "cad_coder_dsl": coder_response.dsl,
                        "attempt": attempt_metadata,
                    },
                )
            )
            attempt_history.append(
                {
                    **attempt_metadata,
                    "cad_adapter": cad_result.adapter_used,
                    "decision_action": decision_result.action.value,
                    "decision_reason": decision_result.reason.value,
                    "decision_detail": decision_result.detail,
                    "semantic_passed": semantic_report.passed,
                    "missing_features": semantic_report.missing_features,
                    "violated_constraints": semantic_report.violated_constraints,
                }
            )
            await self._emit_progress(
                progress_callback,
                subtask=subtask,
                stage="decision",
                status="completed",
                message="품질 검증 결과에 따라 다음 작업을 결정했습니다.",
                detail=_decision_progress_detail(decision_result),
            )

            if (
                not semantic_report.passed
                and decision_result.action == DecisionAction.PROCEED_TO_SLICE
            ):
                raise OrchestratorDecisionError(
                    "Decision Layer attempted to proceed after semantic failure.",
                    decision_result=decision_result,
                )

            if decision_result.action == DecisionAction.PROCEED_TO_SLICE:
                break

            if decision_result.action == DecisionAction.FAIL_WITH_REASON:
                raise _semantic_error(subtask.id, semantic_report, decision_result)

            if decision_result.action == DecisionAction.FALLBACK_TO_PROVIDER:
                raw_target_provider = decision_result.target_provider
                if not raw_target_provider or not raw_target_provider.strip():
                    raise OrchestratorDecisionError(
                        "Provider fallback decision is missing target_provider.",
                        decision_result=decision_result,
                    )
                target_provider = raw_target_provider.strip().lower()
                if self.product_mode and target_provider == "mock":
                    raise OrchestratorDecisionError(
                        "Product mode forbids provider fallback to mock.",
                        decision_result=decision_result,
                    )
                if not self.decision_fallback_model:
                    raise OrchestratorDecisionError(
                        "Provider fallback requires a pinned target model.",
                        decision_result=decision_result,
                    )
                if provider_fallbacks_used >= self.decision_max_provider_fallbacks:
                    raise OrchestratorDecisionError(
                        "Provider fallback budget is exhausted.",
                        decision_result=decision_result,
                    )
                if not self.decision_fallback_provider_available:
                    raise OrchestratorDecisionError(
                        "Configured fallback provider is unavailable.",
                        decision_result=decision_result,
                    )
                if self.decision_provider_policy == ProviderPolicy.LOCAL_ONLY:
                    raise OrchestratorDecisionError(
                        "Provider policy blocks network fallback.",
                        decision_result=decision_result,
                    )
                if (
                    self.decision_quota_remaining is not None
                    and self.decision_quota_remaining <= 0
                ):
                    raise OrchestratorDecisionError(
                        "Provider fallback quota is exhausted.",
                        decision_result=decision_result,
                    )
                estimated_cost = _metadata_float(
                    decision_result.metadata,
                    "estimated_cost_usd",
                )
                if (
                    estimated_cost is not None
                    and self.decision_cost_budget_usd is not None
                    and estimated_cost > self.decision_cost_budget_usd
                ):
                    raise OrchestratorDecisionError(
                        "Provider fallback exceeds the configured cost budget.",
                        decision_result=decision_result,
                    )
                await self._emit_progress(
                    progress_callback,
                    subtask=subtask,
                    stage="provider_fallback",
                    status="started",
                    message="명시된 fallback provider로 CAD 생성을 다시 시도합니다.",
                    detail={
                        "attempt": attempt + 1,
                        "attempt_kind": "provider_fallback",
                        "source_provider": active_provider,
                        "target_provider": target_provider,
                        "target_model": self.decision_fallback_model,
                        "decision_action": decision_result.action.value,
                        "decision_reason": decision_result.reason.value,
                        "estimated_cost_usd": decision_result.metadata.get(
                            "estimated_cost_usd"
                        ),
                        "cost_budget_usd": decision_result.metadata.get(
                            "cost_budget_usd"
                        ),
                    },
                )
                previous_provider = active_provider
                try:
                    active_cad_coder = (
                        self.cad_coder_provider_factory.create_for_provider(
                            CADCoderProviderRequest(
                                source_provider=previous_provider,
                                target_provider=target_provider,
                                target_model=self.decision_fallback_model,
                                trace_id=trace_id,
                            )
                        )
                    )
                except Exception as exc:
                    if isinstance(exc, CADCoderProviderSwitchError):
                        detail = str(exc)
                    else:
                        detail = (
                            "provider factory raised "
                            f"{type(exc).__name__}: {exc}"
                        )
                    raise OrchestratorDecisionError(
                        "Provider fallback factory failed before CAD generation: "
                        f"{detail}",
                        decision_result=decision_result,
                    ) from exc
                provider_fallbacks_used += 1
                source_provider = previous_provider
                attempt_kind = "provider_fallback"
                current_description = subtask.description
                attempt += 1
                await self._emit_progress(
                    progress_callback,
                    subtask=subtask,
                    stage="provider_fallback",
                    status="completed",
                    message="Fallback provider CAD Coder가 준비되었습니다.",
                    detail={
                        "attempt": attempt,
                        "attempt_kind": attempt_kind,
                        "source_provider": source_provider,
                        "target_provider": _cad_coder_provider_name(
                            active_cad_coder
                        ),
                        "target_cad_coder_adapter": active_cad_coder.adapter_name,
                        "target_model": self.decision_fallback_model,
                    },
                )
                continue

            if decision_result.action == DecisionAction.ASK_USER_CLARIFICATION:
                raise OrchestratorDecisionError(
                    "Decision action is not executable in Phase 11C-3D: "
                    f"{decision_result.action.value}.",
                    decision_result=decision_result,
                )

            if decision_result.action == DecisionAction.FALLBACK_TO_TEMPLATE:
                if template_fallbacks_used >= 1:
                    raise _semantic_error(subtask.id, semantic_report, decision_result)
                fallback_model = attempt_metadata.get("model")
                if not isinstance(fallback_model, str):
                    fallback_model = None
                decision_rendered_fallback = self._render_template_fallback(
                    description=subtask.description,
                    requirements=requirements,
                    suggested_template_id=decision_result.target_template_id,
                    fallback_reason=decision_result.reason.value,
                    provider=active_provider,
                    model=fallback_model,
                    repair_attempts=attempt - 1,
                    cad_error=None,
                )
                if decision_rendered_fallback is None:
                    raise OrchestratorDecisionError(
                        "Template fallback decision could not be rendered.",
                        decision_result=decision_result,
                    )
                template_fallbacks_used += 1
                source_provider = active_provider
                attempt_kind = "template_fallback"
                pending_template_fallback = decision_rendered_fallback
                pending_template_reason = decision_result.reason.value
                pending_template_error = None
                attempt += 1
                continue

            if decision_result.action != DecisionAction.RETRY_WITH_SELF_HEALER:
                raise OrchestratorDecisionError(
                    f"Unsupported decision action: {decision_result.action.value}.",
                    decision_result=decision_result,
                )
            if attempt_kind == "provider_fallback":
                raise _semantic_error(subtask.id, semantic_report, decision_result)

            healer_response = await self.self_healer.heal(
                SelfHealerRequest(
                    failed_subtask_id=subtask.id,
                    error_kind="semantic_mismatch",
                    error_message=semantic_report.retry_hint
                    or "Generated CAD missed required semantic features.",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    original_request_summary=(
                        subtask.description
                        + "\nSemantic failure: "
                        + (
                            semantic_report.retry_hint
                            or "Generated CAD missed required semantic features."
                        )
                    ),
                    trace_id=trace_id,
                )
            )
            if healer_response.action != "retry_with_revision":
                raise _semantic_error(subtask.id, semantic_report, decision_result)

            await self._emit_progress(
                progress_callback,
                subtask=subtask,
                stage="semantic_validation",
                status="retrying",
                message="요구사항 누락을 반영해 CAD 명령을 다시 생성합니다.",
                detail={
                    "attempt": decision_result.next_attempt + 1
                    if decision_result.next_attempt is not None
                    else attempt + 1,
                    "max_attempts": max_attempts,
                    "missing_features": semantic_report.missing_features,
                    "violated_constraints": semantic_report.violated_constraints,
                    "reason": healer_response.reason,
                    "decision_action": decision_result.action.value,
                    "decision_reason": decision_result.reason.value,
                    "decision_detail": decision_result.detail,
                },
            )
            revised_instruction = healer_response.revised_instruction
            assert revised_instruction is not None
            current_description = revised_instruction
            attempt_kind = "self_healer_retry"
            source_provider = None
            attempt += 1

        assert semantic_report is not None
        assert decision_result is not None

        await self._emit_progress(
            progress_callback,
            subtask=subtask,
            stage="validate",
            status="started",
            message="출력 가능한 메시인지 검증하는 중입니다.",
            detail={"checks": ("manifold", "wall_thickness", "normal_consistency")},
        )
        report = await self.validator.validate(
            ValidationRequest(
                mesh_path=cad_result.path,
                mesh_format="stl",
                session_id=session_id,
                # Phase 8B: MockMechanicalCADGenerator now emits a closed
                # manifold cube STL, so all three validator checks
                # (MANIFOLD, WALL_THICKNESS, NORMAL_CONSISTENCY) run
                # against the pipeline by default.
                checks=frozenset(
                    {
                        ValidationCheck.MANIFOLD,
                        ValidationCheck.WALL_THICKNESS,
                        ValidationCheck.NORMAL_CONSISTENCY,
                    }
                ),
            )
        )
        if not report.passed:
            error_issues = [i for i in report.issues if i.severity.value == "error"]
            raise RuntimeError(
                f"Mesh validation failed for subtask '{subtask.id}': "
                + "; ".join(i.message for i in error_issues)
            )
        self.logger.info(
            "subtask_validation_passed",
            extra={
                "session_id": session_id,
                "subtask_id": subtask.id,
                "triangle_count": report.triangle_count,
            },
        )
        await self._emit_progress(
            progress_callback,
            subtask=subtask,
            stage="validate",
            status="completed",
            message="메시 검증을 통과했습니다.",
            detail={"triangle_count": report.triangle_count},
        )

        await self._emit_progress(
            progress_callback,
            subtask=subtask,
            stage="slice",
            status="started",
            message="Orca Slicer에 프린터/재료 파라미터를 적용하고 있습니다.",
            detail={"printer": self.printer.name, "material": self.material.name},
        )
        slice_result = await self.slicer.slice(
            SliceRequest(
                mesh_path=cad_result.path,
                mesh_format="stl",
                session_id=session_id,
                output_dir=output_dir,
                printer=self.printer,
                material=self.material,
                process_profile=self.process_profile,
            )
        )
        self.logger.info(
            "subtask_slice_completed",
            extra={
                "session_id": session_id,
                "subtask_id": subtask.id,
                "gcode_path": str(slice_result.gcode_path),
                "adapter": slice_result.adapter_used,
            },
        )
        await self._emit_progress(
            progress_callback,
            subtask=subtask,
            stage="slice",
            status="completed",
            message="G-code 변환이 완료되었습니다.",
            detail={
                "adapter": slice_result.adapter_used,
                "gcode_path": str(slice_result.gcode_path),
            },
        )

        await self._emit_progress(
            progress_callback,
            subtask=subtask,
            stage="manifest",
            status="started",
            message="생성 산출물을 manifest에 기록하는 중입니다.",
        )
        manifest = self._build_manifest(
            session_id=session_id,
            subtask_id=subtask.id,
            artifact_root=artifact_root,
            cad_path=cad_result.path,
            cad_adapter=cad_result.adapter_used,
            cad_metadata=cad_result.metadata,
            gcode_path=slice_result.gcode_path,
            threemf_path=slice_result.threemf_path,
            slicer_adapter=slice_result.adapter_used,
            slicer_version=slice_result.slicer_version,
            requirements=requirements,
            semantic_report=semantic_report,
            decision_result=decision_result,
            attempt_history=tuple(attempt_history),
            trace_id=trace_id,
            approval_gate_id=approval_gate_id,
            plan_snapshot_id=plan_snapshot_id,
        )
        write_manifest(manifest, root=artifact_root)
        self.logger.info(
            "subtask_manifest_written",
            extra={
                "session_id": session_id,
                "subtask_id": subtask.id,
                "artifact_count": len(manifest.artifacts),
            },
        )
        await self._emit_progress(
            progress_callback,
            subtask=subtask,
            stage="manifest",
            status="completed",
            message="STL, G-code, 3MF 산출물 기록이 완료되었습니다.",
            detail={"artifact_count": len(manifest.artifacts)},
        )

        return PipelineArtifacts(
            adapter_cad=cad_result.adapter_used,
            adapter_slicer=slice_result.adapter_used,
            manifest=manifest,
        )

    async def _extract_requirements(
        self,
        *,
        subtask: SubtaskView,
        trace_id: str | None,
        progress_callback: PipelineProgressCallback | None,
    ) -> RequirementSpec | None:
        if self.cad_coder_dsl != "openscad":
            return None
        await self._emit_progress(
            progress_callback,
            subtask=subtask,
            stage="requirements",
            status="started",
            message="사용자 요구사항을 검증 가능한 항목으로 정리하는 중입니다.",
            detail={"adapter": self.requirement_extractor.adapter_name},
        )
        requirements = self.requirement_extractor.extract(
            RequirementExtractionRequest(
                prompt=subtask.description,
                trace_id=trace_id,
            )
        )
        await self._emit_progress(
            progress_callback,
            subtask=subtask,
            stage="requirements",
            status="completed",
            message="요구사항 정리가 완료되었습니다.",
            detail={
                "object_type": requirements.object_type,
                "required_features": requirements.required_features,
                "needs_clarification": requirements.needs_clarification,
            },
        )
        return requirements

    async def _validate_semantics(
        self,
        *,
        subtask: SubtaskView,
        code: str,
        cad_path: Path,
        requirements: RequirementSpec | None,
        trace_id: str | None,
        progress_callback: PipelineProgressCallback | None,
    ) -> SemanticValidationReport:
        if requirements is None:
            return SemanticValidationReport(passed=True, trace_id=trace_id)
        await self._emit_progress(
            progress_callback,
            subtask=subtask,
            stage="semantic_validation",
            status="started",
            message="생성된 모델이 사용자 요구사항을 반영했는지 확인하는 중입니다.",
            detail={"adapter": self.semantic_validator.adapter_name},
        )
        report = self.semantic_validator.validate(
            SemanticValidationRequest(
                code=code,
                requirements=requirements,
                stl_path=str(cad_path),
                trace_id=trace_id,
            )
        )
        status = "completed" if report.passed else "failed"
        message = (
            "요구사항 의미 검증을 통과했습니다."
            if report.passed
            else "생성 모델에서 요구사항 누락을 발견했습니다."
        )
        await self._emit_progress(
            progress_callback,
            subtask=subtask,
            stage="semantic_validation",
            status=status,
            message=message,
            detail={
                "missing_features": report.missing_features,
                "violated_constraints": report.violated_constraints,
                "retry_hint": report.retry_hint,
            },
        )
        return report

    def _matching_template_id(
        self,
        *,
        description: str,
        requirements: RequirementSpec | None,
    ) -> str | None:
        if self.template_fallback_router is None:
            return None
        return self.template_fallback_router.match_template_id(
            TemplateFallbackContext(
                description=description,
                requirements=requirements,
            )
        )

    def _render_template_fallback(
        self,
        *,
        description: str,
        requirements: RequirementSpec | None,
        fallback_reason: str | None,
        provider: str | None,
        model: str | None,
        repair_attempts: int | None,
        cad_error: CADCoderAgentError | None,
        suggested_template_id: str | None = None,
    ) -> TemplateFallbackRender | None:
        if self.template_fallback_router is None:
            return None
        return self.template_fallback_router.render(
            TemplateFallbackContext(
                description=description,
                requirements=requirements,
                suggested_template_id=suggested_template_id,
                fallback_reason=fallback_reason,
                provider=provider,
                model=model,
                repair_attempts=repair_attempts,
            )
        )

    def _build_manifest(
        self,
        *,
        session_id: str,
        subtask_id: str,
        artifact_root: Path,
        cad_path: Path,
        cad_adapter: str,
        cad_metadata: dict[str, object],
        gcode_path: Path,
        threemf_path: Path | None,
        slicer_adapter: str,
        slicer_version: str,
        requirements: RequirementSpec | None,
        semantic_report: SemanticValidationReport,
        decision_result: DecisionResult,
        attempt_history: tuple[dict[str, object], ...],
        trace_id: str | None,
        approval_gate_id: str | None,
        plan_snapshot_id: str | None,
    ) -> SubtaskArtifactManifest:
        refs = [
            build_artifact_ref(
                kind=ArtifactKind.MECHANICAL_MESH,
                path=cad_path,
                session_id=session_id,
                subtask_id=subtask_id,
                producer_adapter=cad_adapter,
                root=artifact_root,
                metadata={
                    "requirements": requirements.model_dump(mode="json")
                    if requirements is not None
                    else None,
                    "semantic_validation": semantic_report.model_dump(mode="json"),
                    "decision": decision_result.model_dump(mode="json"),
                    "cad_runtime": cad_metadata,
                    "attempt_history": attempt_history,
                    "selected_attempt": attempt_history[-1] if attempt_history else None,
                    "template_fallback": _selected_template_fallback(
                        attempt_history
                    ),
                    **_visual_quality_metadata(requirements),
                },
            ),
            build_artifact_ref(
                kind=ArtifactKind.GCODE,
                path=gcode_path,
                session_id=session_id,
                subtask_id=subtask_id,
                producer_adapter=slicer_adapter,
                producer_version=slicer_version,
                root=artifact_root,
            ),
        ]
        if threemf_path is not None:
            refs.append(
                build_artifact_ref(
                    kind=ArtifactKind.THREEMF,
                    path=threemf_path,
                    session_id=session_id,
                    subtask_id=subtask_id,
                    producer_adapter=slicer_adapter,
                    producer_version=slicer_version,
                    root=artifact_root,
                )
            )
        return SubtaskArtifactManifest(
            session_id=session_id,
            subtask_id=subtask_id,
            artifacts=tuple(refs),
            created_at=datetime.now(UTC),
            trace_id=trace_id,
            approval_gate_id=approval_gate_id,
            plan_snapshot_id=plan_snapshot_id,
        )

    async def _emit_progress(
        self,
        callback: PipelineProgressCallback | None,
        *,
        subtask: SubtaskView,
        stage: str,
        status: str,
        message: str,
        detail: dict[str, object] | None = None,
    ) -> None:
        if callback is None:
            return
        await callback(
            PipelineProgress(
                subtask_id=subtask.id,
                kind=subtask.kind,
                stage=stage,
                status=status,
                message=message,
                detail=detail,
            )
        )


@dataclass(frozen=True)
class OrganicPipeline:
    """Organic generator -> manifest pipeline (manifest ADR, Phase 9D close prep).

    Organic outputs are *not* sliced in this build:
    - Real organic adapters (Meshy) emit textured OBJ that the mock
      validator can read but the current Orca slicer pipeline cannot
      consume unsupervised.
    - manifest ADR records ``ORGANIC_MESH`` as a first-class artifact kind
      so UI can render the OBJ even when no G-code exists.

    Once the mechanical+organic merge ADR lands (post Phase 9D), the
    output of this pipeline becomes the input to a merge stage instead
    of a terminal artifact.
    """

    organic: BaseOrganicGenerator
    logger: logging.Logger
    # fallback ADR item 6: mandatory, no default — same reason as MechanicalPipeline.
    prefilter: PipelinePrefilterGate

    async def execute(
        self,
        *,
        session_id: str,
        subtask: SubtaskView,
        output_dir: Path,
        artifact_root: Path,
        trace_id: str | None = None,
        approval_gate_id: str | None = None,
        plan_snapshot_id: str | None = None,
        progress_callback: PipelineProgressCallback | None = None,
    ) -> "OrganicPipelineArtifacts":
        # fallback ADR item 6: FIRST statement — ahead of ``organic.generate`` and
        # ahead of the "생성을 요청했습니다" progress event below.
        _enforce_prefilter(
            self.prefilter,
            prompt=subtask.description,
            route_reasons=ORGANIC_PREFILTER_ROUTE_REASONS,
        )
        await self._emit_progress(
            progress_callback,
            subtask=subtask,
            stage="organic_generate",
            status="started",
            message="유기 형상 메시 생성을 요청했습니다.",
            detail={"format": "obj", "adapter": self.organic.provider_name},
        )
        result = await self.organic.generate(
            OrganicGenerationRequest(
                prompt=subtask.description,
                session_id=session_id,
                output_dir=output_dir,
                format="obj",
            )
        )
        self.logger.info(
            "subtask_organic_completed",
            extra={
                "session_id": session_id,
                "subtask_id": subtask.id,
                "path": str(result.path),
                "adapter": result.adapter_used,
                "format": result.format,
            },
        )
        await self._emit_progress(
            progress_callback,
            subtask=subtask,
            stage="organic_generate",
            status="completed",
            message="유기 형상 메시 생성이 완료되었습니다.",
            detail={"format": result.format, "adapter": result.adapter_used},
        )
        await self._emit_progress(
            progress_callback,
            subtask=subtask,
            stage="manifest",
            status="started",
            message="유기 형상 산출물을 manifest에 기록하는 중입니다.",
        )
        ref = build_artifact_ref(
            kind=ArtifactKind.ORGANIC_MESH,
            path=result.path,
            session_id=session_id,
            subtask_id=subtask.id,
            producer_adapter=result.adapter_used,
            root=artifact_root,
            metadata={
                "format": result.format,
                "generation_time_s": result.generation_time_s,
            },
        )
        manifest = SubtaskArtifactManifest(
            session_id=session_id,
            subtask_id=subtask.id,
            artifacts=(ref,),
            created_at=datetime.now(UTC),
            trace_id=trace_id,
            approval_gate_id=approval_gate_id,
            plan_snapshot_id=plan_snapshot_id,
        )
        write_manifest(manifest, root=artifact_root)
        self.logger.info(
            "subtask_manifest_written",
            extra={
                "session_id": session_id,
                "subtask_id": subtask.id,
                "artifact_count": len(manifest.artifacts),
            },
        )
        await self._emit_progress(
            progress_callback,
            subtask=subtask,
            stage="manifest",
            status="completed",
            message="유기 형상 산출물 기록이 완료되었습니다.",
            detail={"artifact_count": len(manifest.artifacts)},
        )
        return OrganicPipelineArtifacts(
            adapter_organic=result.adapter_used,
            manifest=manifest,
        )

    async def _emit_progress(
        self,
        callback: PipelineProgressCallback | None,
        *,
        subtask: SubtaskView,
        stage: str,
        status: str,
        message: str,
        detail: dict[str, object] | None = None,
    ) -> None:
        if callback is None:
            return
        await callback(
            PipelineProgress(
                subtask_id=subtask.id,
                kind=subtask.kind,
                stage=stage,
                status=status,
                message=message,
                detail=detail,
            )
        )


@dataclass(frozen=True)
class OrganicPipelineArtifacts:
    """Result bundle for an organic subtask (manifest ADR).

    Phase 9D close: legacy flat ``organic_mesh_path`` / ``mesh_format``
    event-payload keys are removed. Consumers must read paths from
    ``manifest.artifacts[].relative_uri``.
    """

    adapter_organic: str
    manifest: SubtaskArtifactManifest

    def to_event_payload(self) -> dict[str, object]:
        return {
            "adapter_organic": self.adapter_organic,
            "manifest": self.manifest.model_dump(mode="json"),
        }


__all__ = [
    "MECHANICAL_PREFILTER_ROUTE_REASONS",
    "ORGANIC_PREFILTER_ROUTE_REASONS",
    "MechanicalPipeline",
    "OrganicPipeline",
    "OrganicPipelineArtifacts",
    "PipelineArtifacts",
    "PipelinePrefilterGate",
    "PipelineProgress",
    "PipelineProgressCallback",
]
