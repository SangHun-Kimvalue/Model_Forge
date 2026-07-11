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
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from apps.orchestrator.exceptions import OrchestratorConfigError
from apps.orchestrator.provider_switch import (
    CADCoderProviderFactory,
    RuntimeCADCoderProviderFactory,
)
from apps.orchestrator.sessions import InMemorySessionStore, SessionStore
from modules.agents.cad_coder.base import BaseCADCoderAgent
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
from modules.llm.factory import LLMProviderSettings, create_llm_provider
from modules.newbie_request.asset_authoring import AssetAuthoringPipeline
from modules.newbie_request.asset_catalog import (
    DecorativeAssetSelector,
    load_decorative_asset_catalog,
)
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
from modules.template.loader import RecipeLoader
from modules.template.renderer import RecipeRenderer
from modules.validator.base import BaseMeshValidator
from modules.validator.factory import ValidatorSettings, create_mesh_validator

_DEFAULT_PRINTER_PROFILE = "Generic Printer standard-Mini 0.4 nozzle"
_DEFAULT_MATERIAL_PROFILE = "Generic Printer PLA @Generic Printer standard-Mini 0.4 nozzle"
_DEFAULT_PROCESS_PROFILE = "generic_printer default @Generic Printer standard-Mini 0.4 nozzle"
_DEFAULT_BED_SIZE_MM = (150.0, 150.0, 150.0)
_CAD_CODER_DSLS: frozenset[str] = frozenset({"cadquery", "build123d", "openscad"})


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
    natural_language_route_entry_enabled: bool = False
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
            natural_language_route_entry_enabled=_bool_from_env(
                source, "ORCHESTRATOR_NATURAL_LANGUAGE_ROUTE_ENTRY", False
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
    cad_coder_provider_factory: CADCoderProviderFactory
    template_fallback_router: TemplateFallbackRouter
    cad: BaseMechanicalCADGenerator
    organic: BaseOrganicGenerator
    validator: BaseMeshValidator
    slicer: BaseSlicer
    llm_assist_facade: LLMAssistFacade | None = None
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("apps.orchestrator")
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
    if (
        (planner is None and planner_settings.adapter == "prompt_based")
        or (cad_coder is None and cad_coder_settings.adapter == "prompt_based")
        or (organic is None and organic_settings.adapter == "blender_script")
    ):
        shared_llm_provider = create_llm_provider(LLMProviderSettings.from_env(), logger=log)

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
        or NaturalLanguageRouteIntegrator(),
        asset_authoring=asset_authoring
        or AssetAuthoringPipeline(
            DecorativeAssetSelector(load_decorative_asset_catalog())
        ),
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
