from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any, Final, Literal, Protocol

from modules.agents.cad_coder.base import BaseCADCoderAgent
from modules.agents.cad_coder.exceptions import (
    CADCoderAgentConfigError,
    CADCoderAgentError,
)
from modules.agents.cad_coder.factory import (
    CADCoderAgentSettings,
    create_cad_coder_agent,
)
from modules.agents.cad_coder.schemas import CADCoderRequest
from modules.cad_mechanical.adapters.openscad import _EXTERNAL_FILE_ACCESS_RE
from modules.llm.base import BaseLLMProvider
from modules.newbie_request.asset_catalog import (
    DecorativeAssetSelector,
    DecorativeAssetSourceType,
)
from modules.newbie_request.asset_draft_schemas import (
    UNVERIFIED_MODEL,
    DraftAssetAuthoringRequest,
    DraftAssetAuthoringResult,
    DraftAssetAuthoringStatus,
    DraftAssetEntry,
    ScadGeneratorIdentity,
    generated_source_sha256,
)
from modules.newbie_request.asset_intake import (
    AssetIntakeRequest,
    AssetIntakeResolver,
    AssetIntakeResult,
    AssetIntakeStatus,
)
from modules.newbie_request.printability import (
    PrintabilityReport,
    printability_gcode_metadata,
    printability_manifest_metadata,
)
from modules.newbie_request.schemas import NewbieRoute, RenderedAssembly, SizeMM

#: Machine-readable ``DraftAssetAuthoringResult.reason`` per blocked pre-filter
#: decision (fallback ADR item 6). The mapping is intentionally closed and does NOT
#: interpolate the variable ``reason_code`` or any human-facing text, so
#: downstream consumers can branch on an exact string. The variable fields stay
#: available in full under ``intake_metadata[CAPABLE_PREFILTER_METADATA_KEY]``.
CAPABLE_PREFILTER_BLOCKED_REASONS: Mapping[str, str] = MappingProxyType(
    {
        "blocked_prohibited": "capable_model_prefilter_blocked_prohibited",
        "manual_review_ip": "capable_model_prefilter_manual_review_ip",
    }
)

#: ``intake_metadata`` key carrying the full structured pre-filter decision.
CAPABLE_PREFILTER_METADATA_KEY = "prefilter"

#: capable-model draft가 요청하는 DSL. **하나의 상수**가 모델에 보내는 요청
#: (``CADCoderRequest.dsl``)과 draft에 기록되는 ``scad_dsl``을 동시에 결정한다.
#: 두 벌이면 어느 날 한쪽만 바뀌어 "요청한 DSL"과 "캐시 키에 들어간 DSL"이 갈라진다 —
#: 그러면 서로 다른 artifact를 낳는 두 job이 같은 키를 공유한다.
_DRAFT_SCAD_DSL: Final[Literal["openscad"]] = "openscad"


class CapablePrefilterDecision(Protocol):
    """Structural view of an IP/abuse pre-filter verdict.

    Declared structurally instead of importing
    ``modules.template.ip_abuse_prefilter.FallbackGateDecision`` because the
    layering direction is ``template -> newbie_request`` and the reverse import
    is rejected by
    ``tests/unit/template/test_fallback_promotion.py::test_newbie_request_does_not_import_template_layer``.
    The concrete predicate is therefore injected from a layer that may depend on
    both (the orchestrator composition root); ``FallbackGateDecision`` satisfies
    this protocol structurally, and ``model_dump`` keeps all four fields
    (``decision`` / ``matched_rule_id`` / ``reason_code`` / ``user_message_ko``)
    intact without this layer restating the schema.
    """

    @property
    def decision(self) -> str: ...

    @property
    def matched_rule_id(self) -> str | None: ...

    @property
    def reason_code(self) -> str: ...

    @property
    def user_message_ko(self) -> str: ...

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]: ...


#: Pre-filter predicate contract: prompt in, structured verdict out, no side
#: effects. Satisfied by
#: ``modules.template.ip_abuse_prefilter.should_invoke_fallback_agent``.
CapablePrefilterGate = Callable[[str], CapablePrefilterDecision]


def _nonblank(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _provider_model_identifier(provider: BaseLLMProvider) -> str:
    """Best-effort model identifier for provenance.

    ``BaseLLMProvider`` does not expose the model in its public contract, so we
    read the provider's pinned model attribute (the same private accessor the
    prompt-based agent already uses for error metadata). When no pinned model
    is available (e.g. a CLI backend-default), the model is recorded as
    ``unverified`` rather than fabricated.
    """
    value = getattr(provider, "_model", None)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return CapableModelDraftGenerator.unverified_model


class AssetAuthoringError(ValueError):
    """Raised when a draft asset cannot be generated deterministically."""


class DraftSandboxViolationError(AssetAuthoringError):
    """Raised when capable-model output references forbidden external files.

    This is a hard rejection (no draft is authored), distinct from a
    recoverable ``CADCoderAgentError``; the pipeline maps it to ASK_USER with a
    dedicated ``sandbox_violation`` reason rather than surfacing a draft.
    """


class DraftPrefilterBlockedError(AssetAuthoringError):
    """Raised when the IP/abuse pre-filter blocks a request *before* the model runs.

    fallback ADR item 6 requires the guard to run before the capable model is
    invoked ("do not generate then catch"), so this is raised ahead of any
    ``generate_code`` call: no agent invocation happens, no draft is authored.
    Mirrors :class:`DraftSandboxViolationError` (hard rejection mapped to
    ASK_USER) but carries the whole ``FallbackGateDecision`` so the two blocked
    verdicts stay distinguishable and every field survives to the user boundary.
    """

    def __init__(self, decision: CapablePrefilterDecision) -> None:
        super().__init__(
            "IP/abuse pre-filter blocked the request before any capable-model "
            f"invocation (decision={decision.decision!r}, "
            f"matched_rule_id={decision.matched_rule_id!r}, "
            f"reason_code={decision.reason_code!r})."
        )
        self.decision = decision


def _prefilter_blocked_reason(decision: CapablePrefilterDecision) -> str:
    """Map a blocked pre-filter decision onto its fixed machine-readable reason.

    Fail-closed: an unmapped decision value is a structural fault, never
    collapsed into a generic ASK_USER reason that would hide a safety verdict.
    """
    reason = CAPABLE_PREFILTER_BLOCKED_REASONS.get(decision.decision)
    if reason is None:
        raise AssetAuthoringError(
            f"Unmapped IP/abuse pre-filter decision {decision.decision!r}; "
            "refusing to collapse an unknown safety verdict into a generic reason."
        )
    return reason


class CapableModelDraftGenerator:
    """Author a review-required draft from capable-model OpenSCAD output.

    Preconditions:
        ``llm_provider`` is a concrete non-mock provider (e.g. the CLI
        provider). The prompt-based CAD coder agent is constructed via the
        real factory; env-based mock fallback is forbidden (DESIGN.md R10).
        ``prefilter`` is mandatory (no default): a capable-model generator
        cannot be constructed without an IP/abuse gate, so no construction path
        can silently invoke the model ungated (fallback ADR item 6).
    Postconditions:
        Returns a ``DraftAssetEntry`` in generated mode (source populated, all
        deterministic spec fields None, review-required, release not allowed).
    Raises:
        DraftPrefilterBlockedError when the IP/abuse pre-filter blocks the
        prompt (raised BEFORE the agent is invoked; no model call, no draft);
        DraftSandboxViolationError when the generated source references
        forbidden external files (no draft authored);
        CADCoderAgentConfigError for configuration faults (fail-fast);
        CADCoderAgentError for recoverable generation failures.
    """

    generator_name = "capable_model_cli_draft_generator"
    renderer_version = "0.1.0"
    #: 정본은 스키마 계층(:data:`UNVERIFIED_MODEL`)에 있다. 기존 공개명은 소비자
    #: 계약이므로 alias로 보존하되 값을 다시 쓰지 않는다 — 두 벌이면 갈라진다.
    unverified_model = UNVERIFIED_MODEL

    def __init__(
        self,
        *,
        llm_provider: BaseLLMProvider,
        prefilter: CapablePrefilterGate,
        model: str | None = None,
        agent_factory: Callable[..., BaseCADCoderAgent] = create_cad_coder_agent,
    ) -> None:
        self._prefilter = prefilter
        self._provider_name = llm_provider.provider_name
        # BaseLLMProvider exposes provider_name but no model in its public
        # contract, and CADCoderResponse drops the response model. Prefer an
        # explicit composition-root model, else read the provider's pinned
        # model best-effort (mirrors PromptBasedCADCoderAgent._provider_model),
        # else record it as unverified so provenance never fabricates a model.
        self._model = _nonblank(model) or _provider_model_identifier(llm_provider)
        self._agent = agent_factory(
            settings=CADCoderAgentSettings(adapter="prompt_based"),
            llm_provider=llm_provider,
        )

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def model(self) -> str:
        return self._model

    @property
    def prefilter(self) -> CapablePrefilterGate:
        """The wired IP/abuse gate (exposed so wiring can be asserted)."""
        return self._prefilter

    @property
    def scad_identity(self) -> ScadGeneratorIdentity:
        """이 생성기가 만들 SCAD job의 **정체성 정본** (fallback ADR Item 5 E2).

        intake는 생성 **전에** 요청 측 캐시 키를 계산해야 하는데, 그러려면 "지금
        무엇으로 생성할 것인가"를 알아야 한다. 그 다섯 값을 intake가 재선언하면
        정체성 정의가 두 벌이 되어, DSL이나 renderer 버전이 바뀌는 날 **생성은 새 키로
        저장하는데 조회는 옛 키로 물어보는** 거짓 miss가 된다. 그래서 실제 생성에 쓰는
        값과 **같은 출처**를 노출한다 — :meth:`generate_draft`도 이 property를 쓴다.
        """

        return ScadGeneratorIdentity(
            draft_generator=self.generator_name,
            draft_renderer_version=self.renderer_version,
            generator_provider=self._provider_name,
            generator_model=self._model,
            scad_dsl=_DRAFT_SCAD_DSL,
        )

    async def generate_draft(
        self, request: DraftAssetAuthoringRequest
    ) -> DraftAssetEntry:
        prompt = request.user_prompt_ko
        # fallback ADR item 6: the IP/abuse guard runs FIRST, ahead of every agent
        # call, so a blocked request costs zero capable-model invocations (no
        # cost, no prompt egress, no abuse surface). Never "generate then catch".
        decision = self._prefilter(prompt)
        if decision.decision != "allow":
            raise DraftPrefilterBlockedError(decision)
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
        response = await self._agent.generate_code(
            CADCoderRequest(
                subtask_id=f"capable_draft_{digest}",
                description=prompt,
                dsl=_DRAFT_SCAD_DSL,
            )
        )
        code = response.code
        if _EXTERNAL_FILE_ACCESS_RE.search(code):
            raise DraftSandboxViolationError(
                "Capable-model OpenSCAD output references forbidden external "
                "file access (include/use/import/surface); refusing to author "
                "a draft asset."
            )
        subject = request.subject or "freeform"
        category = request.category or "freeform"
        style = request.style or "capable_model"
        # fallback ADR Item 5: id는 **artifact-addressed**다. 프롬프트만의 함수로 두면
        # 생성기가 비결정론적이므로 같은 프롬프트의 두 번째 생성물이 첫 번째와 같은
        # id를 받고, 저장 경계에서 조용히 덮어쓰거나(옛 동작) 반드시 실패한다(불변성만
        # 얹은 경우). source digest를 id에 넣으면 **같은 job의 서로 다른 두 생성물이
        # 둘 다 durable하게 남는다** — 그것이 artifact-of-record의 실제 의미다.
        #
        # ``digest``(프롬프트 12자)는 정체성 성분이 **아니고**, 사람이 같은 job의
        # 산출물을 눈으로 묶어 보게 하는 편의다. 정체성은 source digest 32 hex
        # (=128비트 절단)가 진다. digest 계산은 정본 helper를 재사용한다.
        source_digest32 = generated_source_sha256(code)[:32]
        # 생성과 조회가 **같은 출처**의 정체성을 쓴다(E2). 아래에서 다섯 값을 다시
        # 나열하면 그 순간 두 벌이 된다.
        identity = self.scad_identity
        return DraftAssetEntry(
            draft_asset_id=f"draft_freeform_{digest}_{source_digest32}",
            subject=subject,
            category=category,
            style=style,
            source_type=DecorativeAssetSourceType.SELF_AUTHORED,
            license="Model Forge-internal-self-authored-draft",
            source_url_or_owner="External Runtime/Model Forge",
            provenance_note=(
                "Capable-model OpenSCAD draft authored by provider "
                f"'{self._provider_name}' model '{self._model}' via "
                f"{self.generator_name}; review-required, no external "
                "SVG/STL/commercial artwork used."
            ),
            originating_prompt=prompt,
            draft_generator=identity.draft_generator,
            draft_renderer_version=identity.draft_renderer_version,
            review_queue_reason=request.review_queue_reason,
            generated_openscad_source=code,
            # 구조화 provenance(D1). prose ``provenance_note``에도 같은 사실이 있지만
            # 그건 서술이고 이 필드들이 계약이다 — 코드는 여기서만 분기한다.
            generator_provider=identity.generator_provider,
            generator_model=identity.generator_model,
            scad_dsl=identity.scad_dsl,
            scad_origin="generated",
            scad_cache_key=identity.cache_key_for(prompt),
        )


class DeterministicCatKeyringDraftGenerator:
    generator_name = "deterministic_cat_keyring_draft_generator"
    renderer_version = "0.1.0"

    def create_draft(self, request: DraftAssetAuthoringRequest) -> DraftAssetEntry:
        subject = request.subject or _infer_subject(request.user_prompt_ko)
        category = request.category or _infer_category(request.user_prompt_ko)
        if subject != "cat" or category != "keyring":
            raise AssetAuthoringError("Only cat keyring draft authoring is supported")
        style = request.style or "round_face"
        digest = hashlib.sha256(
            f"{request.user_prompt_ko}|{subject}|{category}|{style}".encode()
        ).hexdigest()[:12]
        return DraftAssetEntry(
            draft_asset_id=f"draft_cat_keyring_{style}_{digest}",
            subject=subject,
            category=category,
            style=style,
            source_type=DecorativeAssetSourceType.SELF_AUTHORED,
            license="Model Forge-internal-self-authored-draft",
            source_url_or_owner="External Runtime/Model Forge",
            provenance_note=(
                "Deterministic local draft generated from Model Forge-authored "
                "OpenSCAD primitives; no external SVG/STL/commercial artwork used."
            ),
            originating_prompt=request.user_prompt_ko,
            draft_generator=self.generator_name,
            draft_renderer_version=self.renderer_version,
            review_queue_reason=request.review_queue_reason,
            default_size_mm=SizeMM(width=56.0, depth=54.0, height=4.2),
            min_size_mm=SizeMM(width=45.0, depth=42.0, height=3.0),
            recommended_thickness_mm=4.2,
            required_features=(
                "cat_head",
                "ears",
                "eyes",
                "nose_or_muzzle",
                "whiskers",
                "keyring_hole",
            ),
        )


class DraftAssetKeyringRenderer:
    renderer_name = "draft_asset_keyring_authoring_mvp"
    renderer_version = "0.1.0"

    def render(self, draft: DraftAssetEntry) -> RenderedAssembly:
        if draft.subject != "cat" or draft.category != "keyring":
            raise AssetAuthoringError("DraftAssetKeyringRenderer only supports cat keyrings")
        if draft.recommended_thickness_mm is None:
            raise AssetAuthoringError(
                "DraftAssetKeyringRenderer requires a deterministic spec draft "
                "with recommended_thickness_mm; generated drafts are not "
                "supported by this renderer"
            )
        source = _cat_keyring_source(draft)
        return RenderedAssembly(
            source_catalog_id=draft.draft_asset_id,
            object_type=f"{draft.subject}_keyring",
            category=draft.category,
            source_code=source,
            renderer_name=self.renderer_name,
            renderer_version=self.renderer_version,
            metadata={
                "route": "draft_asset",
                "draft_asset_id": draft.draft_asset_id,
                "asset_lifecycle_status": draft.asset_lifecycle_status.value,
                "subject": draft.subject,
                "style": draft.style,
                "source_type": draft.source_type.value,
                "license": draft.license,
                "source_url_or_owner": draft.source_url_or_owner,
                "provenance_note": draft.provenance_note,
                "draft_generator": draft.draft_generator,
                "draft_renderer_version": draft.draft_renderer_version,
                "review_queue_reason": draft.review_queue_reason,
                "external_asset": False,
                "runtime_catalog_registered": draft.runtime_catalog_registered,
                "visual_quality_status": draft.visual_quality_status.value,
                "legal_review_status": draft.legal_review_status.value,
                "printability_status": draft.printability_status.value,
                "release_allowed": draft.release_allowed,
            },
        )


class AssetAuthoringPipeline:
    """Separates unsupported decorative requests from product runtime fallback."""

    def __init__(
        self,
        runtime_selector: DecorativeAssetSelector,
        *,
        draft_generator: DeterministicCatKeyringDraftGenerator | None = None,
        intake_resolver: AssetIntakeResolver | None = None,
        capable_model_generator: CapableModelDraftGenerator | None = None,
    ) -> None:
        self._draft_generator = draft_generator or DeterministicCatKeyringDraftGenerator()
        self._intake_resolver = intake_resolver or AssetIntakeResolver(
            runtime_selector
        )
        self._capable_model_generator = capable_model_generator

    @property
    def intake_resolver(self) -> AssetIntakeResolver:
        """조립된 intake resolver (배선을 단언할 수 있도록 노출).

        composition root가 큐를 주입했는지 밖에서 물어볼 수 있어야 한다 — 그러지
        않으면 "주석만 바꾸고 배선을 안 하는" 회귀를 테스트가 잡을 수 없다.
        """
        return self._intake_resolver

    def handle(
        self,
        request: DraftAssetAuthoringRequest,
    ) -> DraftAssetAuthoringResult:
        subject = request.subject or _infer_subject(request.user_prompt_ko)
        category = request.category or _infer_category(request.user_prompt_ko)
        style = request.style or _default_style(subject=subject, category=category)
        intake = self._intake_resolver.resolve(
            AssetIntakeRequest(
                user_prompt_ko=request.user_prompt_ko,
                request_id=None,
                subject=subject,
                category=category,
                style=style,
            )
        )
        if intake.status is AssetIntakeStatus.RUNTIME_CATALOG_MATCH:
            return DraftAssetAuthoringResult(
                status=DraftAssetAuthoringStatus.RUNTIME_CATALOG_AVAILABLE,
                reason="verified_runtime_catalog_asset_available",
                runtime_asset_id=intake.runtime_asset_id,
                intake_status=intake.status.value,
                intake_reason=intake.reason.value,
                intake_new_draft_allowed=intake.new_draft_allowed,
                intake_metadata=_intake_metadata(intake),
                candidate_asset_ids=intake.candidate_asset_ids,
                selected_route=NewbieRoute.CURATED_ASSET,
            )
        if intake.status is AssetIntakeStatus.DRAFT_QUEUE_MATCH:
            return DraftAssetAuthoringResult(
                status=DraftAssetAuthoringStatus.DRAFT_REUSE_AVAILABLE,
                reason="existing_draft_asset_available_before_authoring",
                draft_asset=intake.draft_asset,
                intake_status=intake.status.value,
                intake_reason=intake.reason.value,
                intake_new_draft_allowed=intake.new_draft_allowed,
                intake_metadata=_intake_metadata(intake),
                selected_route=NewbieRoute.DRAFT_ASSET,
            )
        if intake.status is AssetIntakeStatus.DUPLICATE_OR_SIMILAR_CANDIDATE:
            return DraftAssetAuthoringResult(
                status=DraftAssetAuthoringStatus.SIMILAR_REFERENCE_AVAILABLE,
                reason="similar_reference_candidate_requires_review_before_new_draft",
                reference_candidate_ids=intake.reference_candidate_ids,
                intake_status=intake.status.value,
                intake_reason=intake.reason.value,
                intake_new_draft_allowed=intake.new_draft_allowed,
                intake_metadata=_intake_metadata(intake),
                selected_route=NewbieRoute.ASK_USER,
            )

        if subject == "cat" and category == "keyring":
            draft = self._draft_generator.create_draft(
                request.model_copy(
                    update={
                        "subject": subject,
                        "category": category,
                        "style": style,
                    }
                )
            )
            return DraftAssetAuthoringResult(
                status=DraftAssetAuthoringStatus.DRAFT_CREATED,
                reason="unsupported_request_routed_to_draft_authoring",
                draft_asset=draft,
                intake_status=intake.status.value,
                intake_reason=intake.reason.value,
                intake_new_draft_allowed=intake.new_draft_allowed,
                intake_metadata=_intake_metadata(intake),
                selected_route=NewbieRoute.DRAFT_ASSET,
            )

        return DraftAssetAuthoringResult(
            status=DraftAssetAuthoringStatus.ASK_USER,
            reason="unsupported_request_requires_clarification_or_manual_review",
            intake_status=intake.status.value,
            intake_reason=intake.reason.value,
            intake_new_draft_allowed=intake.new_draft_allowed,
            intake_metadata=_intake_metadata(intake),
            selected_route=NewbieRoute.ASK_USER,
        )

    async def handle_with_capable_fallback(
        self,
        request: DraftAssetAuthoringRequest,
    ) -> DraftAssetAuthoringResult:
        """Async variant of :meth:`handle` with capable-model terminal fallback.

        Mirrors :meth:`handle` routing exactly for the intake / cat-keyring
        branches (sync ``handle`` is left untouched to avoid caller
        regressions). Only the no-template terminal is extended: when a
        capable-model generator is wired it authors a generated draft, and
        failures collapse into ASK_USER with structured reasons instead of an
        exception (fail-closed; the orchestrator sees exactly two branches,
        DRAFT_CREATED / ASK_USER).
        """
        subject = request.subject or _infer_subject(request.user_prompt_ko)
        category = request.category or _infer_category(request.user_prompt_ko)
        style = request.style or _default_style(subject=subject, category=category)
        intake = self._intake_resolver.resolve(
            AssetIntakeRequest(
                user_prompt_ko=request.user_prompt_ko,
                request_id=None,
                subject=subject,
                category=category,
                style=style,
            )
        )
        if intake.status is AssetIntakeStatus.RUNTIME_CATALOG_MATCH:
            return DraftAssetAuthoringResult(
                status=DraftAssetAuthoringStatus.RUNTIME_CATALOG_AVAILABLE,
                reason="verified_runtime_catalog_asset_available",
                runtime_asset_id=intake.runtime_asset_id,
                intake_status=intake.status.value,
                intake_reason=intake.reason.value,
                intake_new_draft_allowed=intake.new_draft_allowed,
                intake_metadata=_intake_metadata(intake),
                candidate_asset_ids=intake.candidate_asset_ids,
                selected_route=NewbieRoute.CURATED_ASSET,
            )
        if intake.status is AssetIntakeStatus.DRAFT_QUEUE_MATCH:
            return DraftAssetAuthoringResult(
                status=DraftAssetAuthoringStatus.DRAFT_REUSE_AVAILABLE,
                reason="existing_draft_asset_available_before_authoring",
                draft_asset=intake.draft_asset,
                intake_status=intake.status.value,
                intake_reason=intake.reason.value,
                intake_new_draft_allowed=intake.new_draft_allowed,
                intake_metadata=_intake_metadata(intake),
                selected_route=NewbieRoute.DRAFT_ASSET,
            )
        if intake.status is AssetIntakeStatus.DUPLICATE_OR_SIMILAR_CANDIDATE:
            return DraftAssetAuthoringResult(
                status=DraftAssetAuthoringStatus.SIMILAR_REFERENCE_AVAILABLE,
                reason="similar_reference_candidate_requires_review_before_new_draft",
                reference_candidate_ids=intake.reference_candidate_ids,
                intake_status=intake.status.value,
                intake_reason=intake.reason.value,
                intake_new_draft_allowed=intake.new_draft_allowed,
                intake_metadata=_intake_metadata(intake),
                selected_route=NewbieRoute.ASK_USER,
            )

        if subject == "cat" and category == "keyring":
            draft = self._draft_generator.create_draft(
                request.model_copy(
                    update={
                        "subject": subject,
                        "category": category,
                        "style": style,
                    }
                )
            )
            return DraftAssetAuthoringResult(
                status=DraftAssetAuthoringStatus.DRAFT_CREATED,
                reason="unsupported_request_routed_to_draft_authoring",
                draft_asset=draft,
                intake_status=intake.status.value,
                intake_reason=intake.reason.value,
                intake_new_draft_allowed=intake.new_draft_allowed,
                intake_metadata=_intake_metadata(intake),
                selected_route=NewbieRoute.DRAFT_ASSET,
            )

        if self._capable_model_generator is None:
            return self._capable_ask_user_result(
                intake,
                reason="capable_model_generator_unavailable",
            )

        try:
            generated = await self._capable_model_generator.generate_draft(
                request.model_copy(
                    update={
                        "subject": subject,
                        "category": category,
                        "style": style,
                    }
                )
            )
        except CADCoderAgentConfigError:
            # Configuration faults are not recoverable at this boundary: fail
            # fast rather than masking a misconfigured pipeline as ASK_USER.
            raise
        except DraftPrefilterBlockedError as exc:
            # Blocked before any model invocation (fallback ADR item 6). The fixed
            # reason keeps the two verdicts machine-distinguishable and the full
            # decision is preserved so the follow-up manual-review routing phase
            # can identify the case without re-deriving it.
            return self._capable_ask_user_result(
                intake,
                reason=_prefilter_blocked_reason(exc.decision),
                prefilter=exc.decision.model_dump(mode="json"),
            )
        except DraftSandboxViolationError:
            return self._capable_ask_user_result(
                intake,
                reason="sandbox_violation",
            )
        except CADCoderAgentError as exc:
            error_code = exc.error_code or "capable_model_error"
            return self._capable_ask_user_result(
                intake,
                reason=f"capable_model_generation_failed:{error_code}",
            )

        return DraftAssetAuthoringResult(
            status=DraftAssetAuthoringStatus.DRAFT_CREATED,
            reason="unsupported_request_routed_to_capable_model_draft",
            draft_asset=generated,
            intake_status=intake.status.value,
            intake_reason=intake.reason.value,
            intake_new_draft_allowed=intake.new_draft_allowed,
            intake_metadata=_intake_metadata(intake),
            selected_route=NewbieRoute.DRAFT_ASSET,
        )

    def _capable_ask_user_result(
        self,
        intake: AssetIntakeResult,
        *,
        reason: str,
        prefilter: dict[str, object] | None = None,
    ) -> DraftAssetAuthoringResult:
        metadata = _intake_metadata(intake)
        if prefilter is not None:
            metadata[CAPABLE_PREFILTER_METADATA_KEY] = prefilter
        return DraftAssetAuthoringResult(
            status=DraftAssetAuthoringStatus.ASK_USER,
            reason=reason,
            intake_status=intake.status.value,
            intake_reason=intake.reason.value,
            intake_new_draft_allowed=intake.new_draft_allowed,
            intake_metadata=metadata,
            selected_route=NewbieRoute.ASK_USER,
        )


def draft_asset_manifest_metadata(
    *,
    draft: DraftAssetEntry,
    rendered: RenderedAssembly,
    l2_report: object | None = None,
    printability_report: PrintabilityReport | None = None,
    gcode_stage: str = "not_run",
    gcode_reason: str = "draft_asset_review_queue_not_runtime_catalog",
) -> dict[str, object]:
    return {
        "route": "draft_asset",
        "draft_asset": draft.model_dump(mode="json"),
        "source_catalog_id": None,
        "draft_asset_id": draft.draft_asset_id,
        "asset_lifecycle_status": draft.asset_lifecycle_status.value,
        "originating_prompt": draft.originating_prompt,
        "draft_generator": draft.draft_generator,
        "draft_renderer_version": draft.draft_renderer_version,
        "review_queue_reason": draft.review_queue_reason,
        "renderer": rendered.renderer_name,
        "renderer_version": rendered.renderer_version,
        "renderer_metadata": dict(rendered.metadata),
        "visual_quality_required": True,
        "visual_quality_status": draft.visual_quality_status.value,
        "legal_review_status": draft.legal_review_status.value,
        "printability_status": draft.printability_status.value,
        "release_allowed": draft.release_allowed,
        "runtime_catalog_registered": draft.runtime_catalog_registered,
        "product_ready": False,
        "l2_geometry": l2_report.model_dump(mode="json")
        if hasattr(l2_report, "model_dump")
        else l2_report,
        "printability": printability_manifest_metadata(printability_report),
        "gcode": printability_gcode_metadata(
            printability_report,
            requested_stage=gcode_stage,
            requested_reason=gcode_reason,
        ),
    }


def _intake_metadata(intake: AssetIntakeResult) -> dict[str, object]:
    metadata = dict(intake.metadata)
    if intake.candidate_explanations:
        metadata["candidate_explanations"] = [
            explanation.model_dump(mode="json")
            for explanation in intake.candidate_explanations
        ]
    return metadata


def _infer_subject(prompt: str) -> str | None:
    normalized = prompt.casefold()
    if "고양이" in normalized or "cat" in normalized:
        return "cat"
    return None


def _infer_category(prompt: str) -> str | None:
    normalized = prompt.casefold()
    if "키링" in normalized or "keyring" in normalized:
        return "keyring"
    return None


def _default_style(*, subject: str | None, category: str | None) -> str | None:
    if subject == "cat" and category == "keyring":
        return "round_face"
    return None


def _cat_keyring_source(draft: DraftAssetEntry) -> str:
    if draft.recommended_thickness_mm is None:
        raise AssetAuthoringError(
            "cat keyring source requires a deterministic recommended_thickness_mm"
        )
    base_h = draft.recommended_thickness_mm
    relief_h = 1.25
    return "\n".join(
        [
            "$fn = 96;",
            "",
            "module ellipse_2d(x, y, rx, ry) {",
            "  translate([x, y]) scale([rx, ry]) circle(r=1);",
            "}",
            "",
            "module capsule_2d(x1, y1, x2, y2, r) {",
            "  hull() {",
            "    translate([x1, y1]) circle(r=r);",
            "    translate([x2, y2]) circle(r=r);",
            "  }",
            "}",
            "",
            "module raised_disc(x, y, r, z, h) {",
            "  translate([x, y, z]) cylinder(h=h, r=r);",
            "}",
            "",
            "module raised_capsule(x1, y1, x2, y2, r, z, h) {",
            "  translate([0, 0, z]) linear_extrude(height=h)",
            "    capsule_2d(x1, y1, x2, y2, r);",
            "}",
            "",
            "module body_2d() {",
            "  union() {",
            "    ellipse_2d(28.000, 25.000, 21.500, 19.800);",
            "    polygon(points=[[12.0,39.0],[19.2,55.0],[24.5,39.6]]);",
            "    polygon(points=[[31.5,39.6],[36.8,55.0],[44.0,39.0]]);",
            "    capsule_2d(28.000, 44.000, 28.000, 51.500, 3.700);",
            "  }",
            "}",
            "",
            "module face_relief() {",
            f"  raised_disc(22.500, 27.600, 1.400, {base_h - 0.04:.3f}, {relief_h:.3f});",
            f"  raised_disc(33.500, 27.600, 1.400, {base_h - 0.04:.3f}, {relief_h:.3f});",
            f"  raised_disc(28.000, 22.700, 1.250, {base_h + 0.85:.3f}, 0.760);",
            f"  raised_capsule(16.300, 23.300, 23.600, 24.600, 0.330, {base_h + 0.78:.3f}, 0.560);",
            f"  raised_capsule(32.400, 24.600, 39.700, 23.300, 0.330, {base_h + 0.78:.3f}, 0.560);",
            f"  raised_capsule(17.100, 20.400, 23.600, 21.700, 0.300, {base_h + 0.78:.3f}, 0.520);",
            f"  raised_capsule(32.400, 21.700, 38.900, 20.400, 0.300, {base_h + 0.78:.3f}, 0.520);",
            "}",
            "",
            "module main() {",
            "  difference() {",
            "    union() {",
            f"      linear_extrude(height={base_h:.3f}) body_2d();",
            "      face_relief();",
            "    }",
            f"    translate([28.000, 51.800, -0.500]) "
            f"cylinder(h={base_h + relief_h + 1.0:.3f}, r=1.700);",
            "  }",
            "}",
            "",
            "main();",
        ]
    )


__all__ = [
    "CAPABLE_PREFILTER_BLOCKED_REASONS",
    "CAPABLE_PREFILTER_METADATA_KEY",
    "AssetAuthoringError",
    "AssetAuthoringPipeline",
    "CapablePrefilterDecision",
    "CapablePrefilterGate",
    "DeterministicCatKeyringDraftGenerator",
    "DraftAssetKeyringRenderer",
    "DraftPrefilterBlockedError",
    "draft_asset_manifest_metadata",
]
