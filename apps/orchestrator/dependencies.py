"""Orchestrator DI wiring (DESIGN.md §2.3 — Factory + DI).

`build_dependencies` constructs every plugin via its factory, applying
the orchestrator's settings. Tests inject their own ``Dependencies`` to
swap a single component (e.g. a stubbed planner) without rebuilding
the FastAPI app.

Why a dataclass instead of FastAPI Depends?
The runtime graph is not request-scoped — every session shares the same
planner adapter instance across calls — so a single pre-built bundle
matches the actual lifecycle. ``OrchestratorSettings.from_env`` reads
the same env vars module factories already document so there is no
duplicated configuration surface.
"""

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from apps.orchestrator.exceptions import OrchestratorConfigError
from apps.orchestrator.pipeline import PipelinePrefilterGate
from apps.orchestrator.provider_switch import (
    CADCoderProviderFactory,
    RuntimeCADCoderProviderFactory,
)
from apps.orchestrator.sessions import InMemorySessionStore, SessionStore
from modules.agents.cad_coder.base import BaseCADCoderAgent
from modules.agents.cad_coder.exceptions import CADCoderAgentConfigError
from modules.agents.cad_coder.factory import (
    CADCoderAgentSettings,
    create_cad_coder_agent,
)
from modules.agents.cad_coder.schemas import CADDsl
from modules.agents.planner.base import BasePlannerAgent
from modules.agents.planner.factory import (
    PlannerAgentSettings,
    create_planner_agent,
)
from modules.agents.self_healer.base import BaseSelfHealerAgent
from modules.agents.self_healer.factory import (
    SelfHealerAgentSettings,
    create_self_healer_agent,
)
from modules.cad_mechanical.base import BaseMechanicalCADGenerator
from modules.cad_mechanical.factory import (
    CadMechanicalSettings,
    create_mechanical_cad_generator,
)
from modules.llm.base import BaseLLMProvider
from modules.llm.factory import LLMProviderSettings, create_llm_provider
from modules.newbie_request.asset_authoring import (
    AssetAuthoringPipeline,
    CapableModelDraftGenerator,
)
from modules.newbie_request.asset_catalog import (
    DecorativeAssetSelector,
    load_decorative_asset_catalog,
)
from modules.newbie_request.asset_intake import AssetIntakeResolver
from modules.newbie_request.draft_queue import DraftReviewQueue
from modules.newbie_request.llm_assist import LLMAssistFacade
from modules.newbie_request.natural_language import NaturalLanguageRouteIntegrator
from modules.orchestrator_decision.base import BaseDecisionEngine
from modules.orchestrator_decision.rule_based import RuleBasedDecisionEngine
from modules.orchestrator_decision.schemas import ProviderPolicy
from modules.organic_generator.base import BaseOrganicGenerator
from modules.organic_generator.factory import (
    OrganicGeneratorSettings,
    create_organic_generator,
)
from modules.requirements.base import BaseRequirementExtractor
from modules.requirements.factory import (
    RequirementExtractorSettings,
    create_requirement_extractor,
)
from modules.semantic_validator.base import BaseSemanticValidator
from modules.semantic_validator.factory import (
    SemanticValidatorSettings,
    create_semantic_validator,
)
from modules.slicer.base import BaseSlicer
from modules.slicer.factory import SlicerSettings, create_slicer
from modules.template.fallback import TemplateFallbackRouter
from modules.template.ip_abuse_prefilter import should_invoke_fallback_agent
from modules.template.loader import RecipeLoader
from modules.template.renderer import RecipeRenderer
from modules.validator.base import BaseMeshValidator
from modules.validator.factory import ValidatorSettings, create_mesh_validator

_DEFAULT_PRINTER_PROFILE = "Generic Printer standard-Mini 0.4 nozzle"
_DEFAULT_MATERIAL_PROFILE = "Generic Printer PLA @Generic Printer standard-Mini 0.4 nozzle"
_DEFAULT_PROCESS_PROFILE = "generic_printer default @Generic Printer standard-Mini 0.4 nozzle"
_DEFAULT_BED_SIZE_MM = (150.0, 150.0, 150.0)
_DEFAULT_DRAFT_QUEUE_ROOT = ".model_forge/draft-queue"
_CAD_CODER_DSLS: frozenset[str] = frozenset({"cadquery", "build123d", "openscad"})


def _default_review_id() -> str:
    """Unique-per-request manual-review id (fallback ADR item 6, D5).

    Not an idempotency key: re-blocking the same prompt must leave a *separate*
    audit record. The shape satisfies the queue's ``^[a-z0-9][a-z0-9_]*$`` id
    pattern.
    """
    return f"review_{uuid4().hex}"


def _default_created_at() -> datetime:
    """Timezone-aware UTC clock for manual-review records."""
    return datetime.now(UTC)


def _parse_bed_size_mm(raw: str | None) -> tuple[float, float, float]:
    if raw is None or not raw.strip():
        return _DEFAULT_BED_SIZE_MM
    parts = tuple(part.strip() for part in raw.split(",") if part.strip())
    if len(parts) != 3:
        raise OrchestratorConfigError(
            "ORCHESTRATOR_DEFAULT_BED_SIZE_MM must contain exactly three "
            "comma-separated numbers, e.g. '150,150,150'."
        )
    try:
        values = tuple(float(part) for part in parts)
    except ValueError as exc:
        raise OrchestratorConfigError(
            "ORCHESTRATOR_DEFAULT_BED_SIZE_MM must contain numeric values."
        ) from exc
    return (values[0], values[1], values[2])


def _float_from_env(
    source: Mapping[str, str], name: str, default: float
) -> float:
    raw = source.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise OrchestratorConfigError(f"{name} must be a number.") from exc


def _int_from_env(source: Mapping[str, str], name: str, default: int) -> int:
    raw = source.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise OrchestratorConfigError(f"{name} must be an integer.") from exc


def _str_from_env(source: Mapping[str, str], name: str, default: str) -> str:
    raw = source.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


def _optional_float_from_env(
    source: Mapping[str, str], name: str
) -> float | None:
    raw = source.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise OrchestratorConfigError(f"{name} must be a number.") from exc


def _optional_int_from_env(source: Mapping[str, str], name: str) -> int | None:
    raw = source.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise OrchestratorConfigError(f"{name} must be an integer.") from exc


def _bool_from_env(source: Mapping[str, str], name: str, default: bool) -> bool:
    raw = source.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise OrchestratorConfigError(f"{name} must be a boolean.")


def _provider_policy_from_env(source: Mapping[str, str]) -> ProviderPolicy:
    raw = source.get("ORCHESTRATOR_DECISION_PROVIDER_POLICY")
    if raw is None or not raw.strip():
        return ProviderPolicy.FALLBACK_ALLOWED
    try:
        return ProviderPolicy(raw.strip().lower())
    except ValueError as exc:
        allowed = ", ".join(policy.value for policy in ProviderPolicy)
        raise OrchestratorConfigError(
            "ORCHESTRATOR_DECISION_PROVIDER_POLICY must be one of: "
            f"{allowed}."
        ) from exc


def _cad_coder_dsl_from_env(source: Mapping[str, str]) -> CADDsl:
    """Resolve the CAD coder DSL once at orchestrator settings load time."""
    explicit = (source.get("CAD_CODER_DSL") or "").strip().lower()
    if explicit:
        if explicit in _CAD_CODER_DSLS:
            return cast(CADDsl, explicit)
        raise OrchestratorConfigError(
            "CAD_CODER_DSL must be one of: cadquery, build123d, openscad."
        )

    adapter = (source.get("CAD_MECHANICAL_ADAPTER") or "").strip().lower()
    if adapter == "openscad":
        return "openscad"
    if adapter == "build123d":
        return "build123d"
    return "cadquery"


class OrchestratorSettings(BaseModel):
    """App-level settings — currently only delegate-factory selection.

    Per-plugin settings (planner adapter, slicer adapter, …) stay in
    their own factories so changing a plugin's config does not require
    bumping the orchestrator surface.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    log_level: str = "INFO"
    allowed_origins: tuple[str, ...] = ("http://localhost:3000",)
    default_printer_profile: str = _DEFAULT_PRINTER_PROFILE
    default_material_profile: str = _DEFAULT_MATERIAL_PROFILE
    default_process_profile: str | None = _DEFAULT_PROCESS_PROFILE
    default_nozzle_diameter_mm: float = 0.4
    default_bed_size_mm: tuple[float, float, float] = _DEFAULT_BED_SIZE_MM
    default_nozzle_temp_c: int = 205
    default_bed_temp_c: int = 60
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
    decision_max_provider_fallbacks: int = Field(default=1, ge=0)
    template_catalog_root: str = "templates"
    # fallback ADR Item 5 (persistence slice): durable root for review-required
    # drafts authored on the intake next-step boundary. Drafts that only live in
    # the session response vanish when the session ends, which would leave the
    # "draft-only + human review" premise with nothing for a human to review.
    draft_queue_root: str = _DEFAULT_DRAFT_QUEUE_ROOT
    natural_language_route_entry_enabled: bool = False
    capable_draft_route_enabled: bool = False
    llm_assist_shadow_enabled: bool = False

    @classmethod
    def from_env(
        cls, environ: dict[str, str] | None = None
    ) -> "OrchestratorSettings":
        source: Mapping[str, str] = environ if environ is not None else os.environ
        raw_origins = source.get("ORCHESTRATOR_ALLOWED_ORIGINS")
        if raw_origins is None:
            origins: tuple[str, ...] = ("http://localhost:3000",)
        else:
            origins = tuple(
                origin.strip() for origin in raw_origins.split(",") if origin.strip()
            )
        return cls(
            log_level=source.get("ORCHESTRATOR_LOG_LEVEL", "INFO"),
            allowed_origins=origins,
            default_printer_profile=_str_from_env(
                source,
                "ORCHESTRATOR_DEFAULT_PRINTER_PROFILE",
                _DEFAULT_PRINTER_PROFILE,
            ),
            default_material_profile=_str_from_env(
                source,
                "ORCHESTRATOR_DEFAULT_MATERIAL_PROFILE",
                _DEFAULT_MATERIAL_PROFILE,
            ),
            default_process_profile=_str_from_env(
                source,
                "ORCHESTRATOR_DEFAULT_PROCESS_PROFILE",
                _DEFAULT_PROCESS_PROFILE,
            ),
            default_nozzle_diameter_mm=_float_from_env(
                source, "ORCHESTRATOR_DEFAULT_NOZZLE_DIAMETER_MM", 0.4
            ),
            default_bed_size_mm=_parse_bed_size_mm(
                source.get("ORCHESTRATOR_DEFAULT_BED_SIZE_MM")
            ),
            default_nozzle_temp_c=_int_from_env(
                source, "ORCHESTRATOR_DEFAULT_NOZZLE_TEMP_C", 205
            ),
            default_bed_temp_c=_int_from_env(
                source, "ORCHESTRATOR_DEFAULT_BED_TEMP_C", 60
            ),
            cad_coder_dsl=_cad_coder_dsl_from_env(source),
            semantic_max_attempts=_int_from_env(
                source, "ORCHESTRATOR_SEMANTIC_MAX_ATTEMPTS", 2
            ),
            product_mode=_bool_from_env(source, "MODEL_FORGE_PRODUCT_MODE", False),
            decision_cost_budget_usd=_optional_float_from_env(
                source, "ORCHESTRATOR_DECISION_COST_BUDGET_USD"
            ),
            decision_timeout_budget_ms=_optional_int_from_env(
                source, "ORCHESTRATOR_DECISION_TIMEOUT_BUDGET_MS"
            ),
            decision_provider_policy=_provider_policy_from_env(source),
            decision_plan_tier=(
                _str_from_env(source, "ORCHESTRATOR_DECISION_PLAN_TIER", "")
                or None
            ),
            decision_fallback_provider=(
                _str_from_env(source, "ORCHESTRATOR_DECISION_FALLBACK_PROVIDER", "")
                or None
            ),
            decision_fallback_model=(
                _str_from_env(source, "ORCHESTRATOR_DECISION_FALLBACK_MODEL", "")
                or None
            ),
            decision_fallback_provider_available=_bool_from_env(
                source, "ORCHESTRATOR_DECISION_FALLBACK_PROVIDER_AVAILABLE", True
            ),
            decision_quota_remaining=_optional_float_from_env(
                source, "ORCHESTRATOR_DECISION_QUOTA_REMAINING"
            ),
            decision_max_provider_fallbacks=_int_from_env(
                source, "ORCHESTRATOR_DECISION_MAX_PROVIDER_FALLBACKS", 1
            ),
            template_catalog_root=_str_from_env(
                source, "ORCHESTRATOR_TEMPLATE_CATALOG_ROOT", "templates"
            ),
            draft_queue_root=_str_from_env(
                source,
                "ORCHESTRATOR_DRAFT_QUEUE_ROOT",
                _DEFAULT_DRAFT_QUEUE_ROOT,
            ),
            natural_language_route_entry_enabled=_bool_from_env(
                source, "ORCHESTRATOR_NATURAL_LANGUAGE_ROUTE_ENTRY", False
            ),
            capable_draft_route_enabled=_bool_from_env(
                source, "CUBIFORGE_CAPABLE_DRAFT_ROUTE", False
            ),
            llm_assist_shadow_enabled=_bool_from_env(
                source, "ORCHESTRATOR_LLM_ASSIST_SHADOW", False
            ),
        )


@dataclass
class Dependencies:
    """Bundle of orchestrator-owned singletons.

    ``cad``, ``validator``, and ``slicer`` are the Phase 8A pipeline
    adapters. ``build_dependencies`` constructs them from their respective
    factory settings; tests may inject mock instances directly.
    """

    settings: OrchestratorSettings
    sessions: SessionStore
    planner: BasePlannerAgent
    cad_coder: BaseCADCoderAgent
    self_healer: BaseSelfHealerAgent
    requirement_extractor: BaseRequirementExtractor
    semantic_validator: BaseSemanticValidator
    decision_engine: BaseDecisionEngine
    natural_language_router: NaturalLanguageRouteIntegrator
    asset_authoring: AssetAuthoringPipeline
    # Mandatory (never ``None``): a missing queue must not degrade into a silent
    # no-op that answers ``draft_created`` while nothing was written (R10).
    # 이 큐는 ``AssetIntakeResolver``에도 **주입된다** (fallback ADR Item 5 재사용 배선):
    # 앞선 캐시 슬라이스가 SCAD 캐시 키와 provenance를 고정했고, 이제 그 키를 실제로
    # 쓰는 첫 경로가 열렸다. 그래서 큐에 이미 있는 draft는 재사용되고
    # (``DRAFT_QUEUE_MATCH`` -> ``DRAFT_REUSE_AVAILABLE``), 사람이 REJECT한 형상은
    # 배제된다. 재사용은 draft를 **제시**할 뿐 실행 권한을 주지 않는다.
    draft_queue: DraftReviewQueue
    cad_coder_provider_factory: CADCoderProviderFactory
    template_fallback_router: TemplateFallbackRouter
    cad: BaseMechanicalCADGenerator
    organic: BaseOrganicGenerator
    validator: BaseMeshValidator
    slicer: BaseSlicer
    llm_assist_facade: LLMAssistFacade | None = None
    # fallback ADR Item 6 (manual-review routing): identity/clock seams for the
    # pre-filter manual-review record. They live here — not as graph arguments
    # or module globals — so tests get determinism through the same DI door as
    # ``draft_queue``, and production keeps a unique id + tz-aware UTC clock.
    # ``created_at_factory`` returns a ``datetime``; the caller renders it with
    # ``.isoformat()``.
    review_id_factory: Callable[[], str] = field(
        default_factory=lambda: _default_review_id
    )
    created_at_factory: Callable[[], datetime] = field(
        default_factory=lambda: _default_created_at
    )
    # fallback ADR Item 6 (execution pipelines): ONE composition root owns the
    # binding of the deterministic IP/abuse predicate — the same one already
    # bound into ``CapableModelDraftGenerator`` above. Holding it here rather
    # than importing the predicate inside ``graph.py`` keeps a single wiring
    # site (two would drift), and gives tests the same DI seam as
    # ``draft_queue``. The default is the *real* predicate, so the fail-closed
    # direction is preserved: an unconfigured deployment gets the guard, not a
    # bypass.
    prefilter: PipelinePrefilterGate = field(
        default_factory=lambda: should_invoke_fallback_agent
    )
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("apps.orchestrator")
    )


def _capable_model_identifier(settings: LLMProviderSettings) -> str | None:
    """Provider-aware model identifier for capable-model provenance.

    Mirrors the per-provider model selection in ``create_llm_provider`` so a
    stale ``CLI_MODEL`` never leaks into a non-CLI provider's provenance. When
    the provider does not pin a model here, the generator falls back to the
    constructed provider's own ``_model`` (or records it as ``unverified``).
    """
    if settings.provider == "cli":
        return settings.cli_model
    if settings.provider == "gemini":
        return settings.gemini_model or settings.model
    return settings.model


def _build_capable_model_generator(
    provider: BaseLLMProvider | None,
    settings: LLMProviderSettings,
) -> CapableModelDraftGenerator | None:
    """Wire the capable-model generator only from a concrete non-mock provider.

    The generator is inherently ``prompt_based``; backing it with a mock
    provider would silently bypass the CAD coder factory's R10 defense
    (``prompt_based`` + mock is rejected there). When no real provider exists
    the generator is simply absent (deterministic path only, no regression);
    when a real adapter forced provider construction but ``LLM_PROVIDER=mock``,
    that is a contradictory configuration and we fail fast rather than authoring
    with a mock model.
    """
    if provider is None:
        return None
    if settings.provider == "mock":
        raise CADCoderAgentConfigError(
            "Capable-model draft authoring is prompt_based and requires a "
            "concrete non-mock LLM_PROVIDER (e.g. cli or anthropic). "
            "LLM_PROVIDER=mock cannot back the capable generator; set a real "
            "provider, or use mock adapters throughout so the capable "
            "fallback stays disabled (DESIGN.md R10 no silent mock fallback)."
        )
    return CapableModelDraftGenerator(
        llm_provider=provider,
        # fallback ADR item 6: the composition root binds the deterministic IP/abuse
        # predicate as the generator's gate. ``prefilter`` is a mandatory
        # constructor argument, so no capable generator can be assembled ungated,
        # and the layering direction (template -> newbie_request) stays intact.
        prefilter=should_invoke_fallback_agent,
        model=_capable_model_identifier(settings),
    )


def _build_asset_authoring(
    *,
    draft_queue: DraftReviewQueue,
    capable_model_generator: CapableModelDraftGenerator | None,
) -> AssetAuthoringPipeline:
    """draft 재사용이 **켜진** authoring 파이프라인을 조립한다 (fallback ADR Item 5 E1).

    ``AssetIntakeResolver``는 큐를 optional로 받고 기본값은 ``None``으로 **유지**된다 —
    유닛 테스트가 큐 없는 resolver를 쓰기 때문이다. 다만 **프로덕션 조립에서는 항상
    주입된다**. 그 둘의 차이가 이 함수의 존재 이유다.

    ``generator_identity``가 capable 생성기에서 오는 이유(E2): generated draft는
    "같은 SCAD 생성 job인가"(캐시 키)로만 후보가 되는데, intake는 생성 **전에**
    일어나므로 "지금 무엇으로 생성할 것인가"를 생성기에서 받아와야 한다. 여기서 DSL이나
    생성기 이름을 다시 적으면 정체성 정의가 두 벌이 된다.

    capable 생성기가 없는 구성(결정론만)에서는 정체성이 ``None``이고, 그러면
    **generated draft 재사용을 하지 않는다**(spec draft 매칭만 동작). fail-closed로
    옳다 — 무엇으로 생성할지 모르면서 "같은 job"이라고 판정할 수 없다.
    """

    runtime_selector = DecorativeAssetSelector(load_decorative_asset_catalog())
    return AssetAuthoringPipeline(
        runtime_selector,
        intake_resolver=AssetIntakeResolver(
            runtime_selector,
            draft_queue=draft_queue,
            generator_identity=(
                capable_model_generator.scad_identity
                if capable_model_generator is not None
                else None
            ),
        ),
        capable_model_generator=capable_model_generator,
    )


def _reject_split_draft_queue(
    asset_authoring: AssetAuthoringPipeline,
    draft_queue: DraftReviewQueue,
) -> None:
    """번들에 draft 큐가 **둘**이면 조립 시점에 거부한다 (fallback ADR Item 5).

    호출자가 ``asset_authoring``만 주입하고 그 파이프라인의 resolver가 **다른** 큐
    객체를 들고 있으면, ``/chat``은 큐 A에서 재사용 후보를 찾고 ``graph``는 큐 B에
    저장한다 — 같은 요청이 영원히 재사용되지 않고 draft가 **중복 생성**된다. 두 큐가
    우연히 같은 루트를 가리켜도 마찬가지로 위험하다: 그건 조립이 보장한 것이 아니라
    호출자가 맞춰준 것이라 한쪽 설정만 바뀌는 날 조용히 갈라진다.

    ``None``도 **거부한다**. 그건 "두 큐"는 아니지만 결과가 같은 갈라짐이다: 번들의
    mandatory ``draft_queue``에는 계속 **저장하면서** intake는 그 큐를 **전혀 읽지
    않는다** — 명시적 feature flag가 아니라 객체 내부의 ``None`` 하나로 재사용이 조용히
    꺼진다. 쓰기 큐와 읽기 큐가 어긋나지 않게 하는 것이 이 가드의 목적이므로 절반만
    막을 이유가 없다.

    ⚠️ 이 가드는 **프로덕션 조립(:func:`build_dependencies`)에서만** 호출된다.
    ``AssetIntakeResolver``의 ``draft_queue=None`` 기본값은 그대로 유지되고, 큐 없는
    resolver를 쓰는 유닛 테스트는 파이프라인을 직접 조립하므로 영향을 받지 않는다.

    조용한 저하 대신 fail-fast인 이유는 R10(silent fallback 금지)과 같다 — 어긋난 큐를
    가진 번들은 **정상 동작처럼 보이면서** 재사용만 조용히 죽는다.
    """

    resolver_queue = asset_authoring.intake_resolver.draft_queue
    if resolver_queue is draft_queue:
        return
    if resolver_queue is None:
        raise OrchestratorConfigError(
            "Injected asset_authoring has no DraftReviewQueue on its intake resolver, "
            f"but the bundle persists drafts into {draft_queue.root}. Intake would "
            "never read the queue the orchestrator writes to, so draft reuse would be "
            "off while everything else looked wired — a silent degradation, not a "
            "configuration. Pass the bundle's DraftReviewQueue to the pipeline's "
            "AssetIntakeResolver."
        )
    raise OrchestratorConfigError(
        "Injected asset_authoring carries a different DraftReviewQueue than the "
        f"bundle's draft_queue (resolver root={resolver_queue.root}, bundle root="
        f"{draft_queue.root}). Intake would look for reusable drafts in one queue "
        "while the orchestrator persists into the other, so the same request would "
        "never be reused and drafts would be authored again on every turn. Pass the "
        "same DraftReviewQueue object to both."
    )


def build_dependencies(
    settings: OrchestratorSettings | None = None,
    *,
    sessions: SessionStore | None = None,
    planner: BasePlannerAgent | None = None,
    cad_coder: BaseCADCoderAgent | None = None,
    self_healer: BaseSelfHealerAgent | None = None,
    requirement_extractor: BaseRequirementExtractor | None = None,
    semantic_validator: BaseSemanticValidator | None = None,
    decision_engine: BaseDecisionEngine | None = None,
    natural_language_router: NaturalLanguageRouteIntegrator | None = None,
    asset_authoring: AssetAuthoringPipeline | None = None,
    draft_queue: DraftReviewQueue | None = None,
    review_id_factory: Callable[[], str] | None = None,
    created_at_factory: Callable[[], datetime] | None = None,
    prefilter: PipelinePrefilterGate | None = None,
    llm_assist_facade: LLMAssistFacade | None = None,
    cad_coder_provider_factory: CADCoderProviderFactory | None = None,
    template_fallback_router: TemplateFallbackRouter | None = None,
    cad: BaseMechanicalCADGenerator | None = None,
    organic: BaseOrganicGenerator | None = None,
    validator: BaseMeshValidator | None = None,
    slicer: BaseSlicer | None = None,
    logger: logging.Logger | None = None,
) -> Dependencies:
    """Compose the orchestrator dependencies, allowing per-component override."""
    effective_settings = settings or OrchestratorSettings.from_env()
    if (
        effective_settings.capable_draft_route_enabled
        and not effective_settings.natural_language_route_entry_enabled
    ):
        raise OrchestratorConfigError(
            "CUBIFORGE_CAPABLE_DRAFT_ROUTE requires ORCHESTRATOR_NATURAL_LANGUAGE_ROUTE_ENTRY."
        )
    log = logger or logging.getLogger("apps.orchestrator")
    try:
        log.setLevel(effective_settings.log_level.upper())
    except ValueError as exc:
        raise OrchestratorConfigError(
            f"Invalid ORCHESTRATOR_LOG_LEVEL: {effective_settings.log_level!r}."
        ) from exc
    planner_settings = PlannerAgentSettings.from_env()
    cad_coder_settings = CADCoderAgentSettings.from_env()
    organic_settings = OrganicGeneratorSettings.from_env()
    shared_llm_provider = None
    shared_llm_settings = LLMProviderSettings.from_env()
    if (
        (planner is None and planner_settings.adapter == "prompt_based")
        or (cad_coder is None and cad_coder_settings.adapter == "prompt_based")
        or (organic is None and organic_settings.adapter == "blender_script")
        or (effective_settings.capable_draft_route_enabled and asset_authoring is None)
    ):
        shared_llm_provider = create_llm_provider(shared_llm_settings, logger=log)
    if (
        effective_settings.capable_draft_route_enabled
        and asset_authoring is None
        and not _capable_model_identifier(shared_llm_settings)
    ):
        raise OrchestratorConfigError(
            "CUBIFORGE_CAPABLE_DRAFT_ROUTE requires a pinned capable model. "
            "Set the model variable for the selected LLM provider."
        )
    capable_model_generator = (
        _build_capable_model_generator(shared_llm_provider, shared_llm_settings)
        if asset_authoring is None
        else None
    )
    # 큐를 ``Dependencies(...)`` 인자 자리에서 만들면 intake resolver가 그것을 볼 수
    # 없다(같은 호출의 다른 인자다). 재사용 배선은 **같은 큐 객체**를 두 곳이 공유하는
    # 것이 핵심이므로 — 두 인스턴스면 "쓴 곳과 읽는 곳이 다른 루트"라는 조용한
    # 갈라짐이 생긴다 — 여기서 한 번 만든다.
    effective_draft_queue = draft_queue or DraftReviewQueue(
        Path(effective_settings.draft_queue_root)
    )
    # 번들에 authoring 파이프라인은 **하나**다. ``NaturalLanguageRouteIntegrator``의
    # 기본값은 자기 파이프라인을 새로 만드는데, 재사용이 켜진 뒤로 그건 갈라짐이다:
    # ``/chat``(라우터 소유 파이프라인)은 "새 초안을 만들 수 있다"고 답하고
    # ``/intake-next-step``(``deps.asset_authoring``)은 재사용을 돌려줘 409로 막는
    # 막다른 길이 된다. 같은 큐를 보는 파이프라인 하나를 두 경로가 공유해야
    # ``/chat``이 곧바로 "기존 초안 검토"를 제시한다.
    effective_asset_authoring = asset_authoring or _build_asset_authoring(
        draft_queue=effective_draft_queue,
        capable_model_generator=capable_model_generator,
    )
    _reject_split_draft_queue(effective_asset_authoring, effective_draft_queue)

    return Dependencies(
        settings=effective_settings,
        sessions=sessions or InMemorySessionStore(),
        planner=planner
        or create_planner_agent(
            planner_settings, logger=log, llm_provider=shared_llm_provider
        ),
        cad_coder=cad_coder
        or create_cad_coder_agent(
            cad_coder_settings, logger=log, llm_provider=shared_llm_provider
        ),
        self_healer=self_healer
        or create_self_healer_agent(SelfHealerAgentSettings.from_env(), logger=log),
        requirement_extractor=requirement_extractor
        or create_requirement_extractor(RequirementExtractorSettings.from_env()),
        semantic_validator=semantic_validator
        or create_semantic_validator(SemanticValidatorSettings.from_env()),
        decision_engine=decision_engine or RuleBasedDecisionEngine(),
        natural_language_router=natural_language_router
        or NaturalLanguageRouteIntegrator(asset_authoring=effective_asset_authoring),
        asset_authoring=effective_asset_authoring,
        draft_queue=effective_draft_queue,
        review_id_factory=review_id_factory or _default_review_id,
        created_at_factory=created_at_factory or _default_created_at,
        prefilter=prefilter or should_invoke_fallback_agent,
        llm_assist_facade=llm_assist_facade,
        cad_coder_provider_factory=cad_coder_provider_factory
        or RuntimeCADCoderProviderFactory(logger=log),
        template_fallback_router=template_fallback_router
        or TemplateFallbackRouter(
            loader=RecipeLoader(Path(effective_settings.template_catalog_root)),
            renderer=RecipeRenderer(),
        ),
        cad=cad or create_mechanical_cad_generator(CadMechanicalSettings.from_env(), logger=log),
        organic=organic
        or create_organic_generator(
            organic_settings, logger=log, llm_provider=shared_llm_provider
        ),
        validator=validator or create_mesh_validator(ValidatorSettings.from_env(), logger=log),
        slicer=slicer or create_slicer(SlicerSettings.from_env(), logger=log),
        logger=log,
    )


__all__ = [
    "Dependencies",
    "OrchestratorSettings",
    "build_dependencies",
]
