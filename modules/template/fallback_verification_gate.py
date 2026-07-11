"""Deterministic automated verification gate for capable-model fallback drafts.

ADR-0015 Open Item 1 (verification gate, load-bearing), sub-phase 12Z-H1.

A capable-model fallback may emit *freeform OpenSCAD source* when no template
matches.  This module composes the deterministic checks that are already
scattered across the codebase (ADR-0001 sandbox tokens, static complexity,
post-compile L2 geometry, printability) into a single gate that blocks only
*blank / unsafe / unprintable* drafts and — when it passes — yields a
**draft-only** artifact (``review_required``) and nothing else.

What this gate is NOT
---------------------
The automated gate **never certifies functional correctness** (involute gear
geometry, part fit, intended curvature).  Determinism cannot judge intent
(R12: a successful STL/compile is not user-intent or product PASS).  Functional
acceptance is the authority of a human reviewer (ADR-0015).  This invariant is
pinned structurally: ``FallbackGateVerdict.functional_correctness_certified``
is ``Literal[False]`` — code cannot express "functional PASS".

SQG L1/L2 mapping for freeform SCAD (D2)
----------------------------------------
The newbie L1 (schema / component-enum) does **not** apply to freeform SCAD.
For this gate:
    * freeform "L1" (pre-execution, source level) = blank/source-presence,
      ADR-0001 sandbox tokens, static complexity cap.
    * freeform "L2" (post-compile, mesh level)    = geometry presence smoke
      (``evaluate_assembly_stl``) + printability (``evaluate_printability``).

Seams (D3 / D5)
---------------
* Compilation is an **injected seam** (callable, or a pre-compiled STL path on
  the request).  Unit tests pass a fake compile seam / mesh fixture and never
  need OpenSCAD or Docker.  Real OpenSCAD compilation is opt-in live only
  (``MODEL_FORGE_RUN_*`` / ``tests/integration``); when not run it is NOT CLAIMED.
* watertight/wall is an **injected synchronous ``mesh_validator`` seam**
  (default ``None``).  The deterministic core does NOT check watertightness
  (``L2GeometryReport.watertight is None``; ``PrintabilityGuardPolicy
  .require_watertight`` defaults False).  When the seam is absent the verdict
  records ``watertight_checked=False`` and watertight is never used as a PASS
  basis (R12, no silent claim).  The real ``TrimeshValidatorAdapter`` (optional
  ``trimesh`` extra, async) is intentionally NOT imported here — integration
  opt-in only.

Known gate gap
--------------
There is no deterministic *overhang* check in the codebase (overhang only
exists in prompt/mock layers).  This gate does not invent one; it records the
gap honestly in ``FallbackGateVerdict.gate_gaps`` rather than implying coverage.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Reuse the canonical OpenSCAD sandbox-token policy (single source of truth).
# NOTE: the phase prompt's attachment point named ``_FORBIDDEN_CALL_NAMES``,
# but that frozenset is the *Python* sandbox call-block (eval/exec/__import__…)
# for the CadQuery/build123d runner.  The OpenSCAD equivalent — the
# include/use/import/surface external-file directives this gate must catch — is
# ``_EXTERNAL_FILE_ACCESS_RE``, the runtime enforcer in the OpenSCAD adapter.
# Importing it keeps the pre-flight gate and the runtime adapter in lockstep
# instead of duplicating a security-critical pattern.
from modules.cad_mechanical.adapters.openscad import _EXTERNAL_FILE_ACCESS_RE
from modules.cad_mechanical.complexity_guard import (
    ComplexityLimits,
    check_scad_complexity,
)
from modules.newbie_request.asset_catalog import (
    DecorativeAssetSourceType,
)
from modules.newbie_request.asset_draft_schemas import (
    AssetLifecycleStatus,
    DraftAssetEntry,
)
from modules.newbie_request.draft_queue import DraftReviewQueue
from modules.newbie_request.geometry import evaluate_assembly_stl
from modules.newbie_request.printability import (
    PrintabilityGuardPolicy,
    PrintabilityStatus,
    evaluate_printability,
)
from modules.newbie_request.schemas import L2GeometryReport, SizeMM

__all__ = [
    "CompileScadSeam",
    "FallbackDraftDescriptor",
    "FallbackGateCheckStatus",
    "FallbackGateError",
    "FallbackGateOutcome",
    "FallbackGateRequest",
    "FallbackGateVerdict",
    "FallbackVerificationGate",
    "GateCheckResult",
    "MeshSoundnessProbe",
    "MeshSoundnessResult",
    "OVERHANG_GATE_GAP",
]

#: Honest, always-recorded gap: no deterministic overhang predicate exists.
OVERHANG_GATE_GAP = (
    "overhang: no deterministic overhang check exists (prompt/mock layers only); "
    "NOT enforced by this gate (R12)"
)


class FallbackGateCheckStatus(StrEnum):
    """Per-check outcome.  ``NOT_CHECKED`` is honest absence, not a pass."""

    PASSED = "passed"
    BLOCKED = "blocked"
    NOT_CHECKED = "not_checked"


class GateCheckResult(BaseModel):
    """Result of a single deterministic check stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    status: FallbackGateCheckStatus
    detail: str = ""


class MeshSoundnessResult(BaseModel):
    """Return contract of an injected synchronous ``mesh_validator`` seam."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    watertight: bool
    wall_thickness_ok: bool | None = None
    detail: str = ""


class CompileScadSeam(Protocol):
    """Injected compile seam: OpenSCAD source + workdir -> compiled STL path.

    Implementations may shell out to OpenSCAD/Docker (live, opt-in) or return a
    fixture path (unit tests).  Raising signals a compile failure (the gate
    records a BLOCK rather than letting the exception escape silently).
    """

    def __call__(self, scad_source: str, workdir: Path, /) -> Path: ...


class MeshSoundnessProbe(Protocol):
    """Injected synchronous watertight/wall seam (default absent).

    The real ``TrimeshValidatorAdapter`` is async + optional-extra and is NOT
    used here; this seam keeps watertightness out of the deterministic core.
    """

    def __call__(self, stl_path: Path, /) -> MeshSoundnessResult: ...


class FallbackDraftDescriptor(BaseModel):
    """Descriptive metadata required to materialize a draft-only entry on PASS.

    The gate is generator-agnostic and only sees ``scad_source``; the catalog
    fields a ``DraftAssetEntry`` needs must be supplied by the caller.  All
    lifecycle-bearing fields (status/release/registration) are forced closed by
    the gate, not taken from here.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    draft_asset_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_]*$")
    subject: str = Field(min_length=1)
    category: str = Field(min_length=1)
    style: str = Field(min_length=1)
    source_type: DecorativeAssetSourceType
    license: str = Field(min_length=1)
    source_url_or_owner: str = Field(min_length=1)
    provenance_note: str = Field(min_length=1)
    originating_prompt: str = Field(min_length=1)
    draft_generator: str = Field(min_length=1)
    draft_renderer_version: str = Field(min_length=1)
    review_queue_reason: str = Field(min_length=1)
    default_size_mm: SizeMM
    min_size_mm: SizeMM
    recommended_thickness_mm: float = Field(gt=0)
    required_features: tuple[str, ...] = Field(min_length=1)


class FallbackGateRequest(BaseModel):
    """One gate evaluation: freeform SCAD source + compile input + optional draft.

    Exactly one compile path must be available at evaluation time: either
    ``precompiled_stl_path`` is set, or the gate was constructed with a
    ``compile_scad`` seam.  ``draft_descriptor`` is optional — when absent a
    PASS verdict simply carries no materialized draft (predicate vs. output).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scad_source: str
    precompiled_stl_path: Path | None = None
    draft_descriptor: FallbackDraftDescriptor | None = None


class FallbackGateVerdict(BaseModel):
    """Outcome of the deterministic automated gate.

    The automated gate only attests "not blank / not unsafe / not unprintable".
    It structurally cannot attest functional correctness.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    automated_gate_passed: bool
    #: Pinned by type — the automated gate never certifies functional intent.
    functional_correctness_certified: Literal[False] = False

    source_presence: GateCheckResult
    sandbox_tokens: GateCheckResult
    static_complexity: GateCheckResult
    compilation: GateCheckResult
    l2_geometry: GateCheckResult
    printability: GateCheckResult

    #: watertight/wall is the injected seam (D5).  ``checked=False`` means the
    #: seam was absent and watertight is NOT a basis for the PASS decision.
    watertight_checked: bool = False
    watertight_passed: bool | None = None

    blocking_reasons: tuple[str, ...] = ()
    gate_gaps: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _consistency(self) -> FallbackGateVerdict:
        if not self.watertight_checked and self.watertight_passed is not None:
            raise ValueError(
                "watertight_passed must be None when watertight_checked is False"
            )
        deterministic_checks = (
            self.source_presence,
            self.sandbox_tokens,
            self.static_complexity,
            self.compilation,
            self.l2_geometry,
            self.printability,
        )
        any_blocked = any(
            check.status is FallbackGateCheckStatus.BLOCKED
            for check in deterministic_checks
        )
        any_not_checked = any(
            check.status is FallbackGateCheckStatus.NOT_CHECKED
            for check in deterministic_checks
        )
        watertight_failed = self.watertight_checked and self.watertight_passed is False
        expected_pass = not (any_blocked or any_not_checked or watertight_failed)
        if self.automated_gate_passed != expected_pass:
            raise ValueError(
                "automated_gate_passed contradicts the recorded check statuses"
            )
        if self.automated_gate_passed and self.blocking_reasons:
            raise ValueError("a passing verdict must carry no blocking_reasons")
        if not self.automated_gate_passed and not self.blocking_reasons:
            raise ValueError("a blocking verdict must record at least one reason")
        return self


class FallbackGateOutcome(BaseModel):
    """Verdict plus the draft-only artifact (only ever a DRAFT, never release)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: FallbackGateVerdict
    draft: DraftAssetEntry | None = None
    draft_saved_path: Path | None = None

    @model_validator(mode="after")
    def _draft_requires_pass(self) -> FallbackGateOutcome:
        if self.draft is not None and not self.verdict.automated_gate_passed:
            raise ValueError("a draft may only be emitted for a passing verdict")
        if self.draft is None and self.draft_saved_path is not None:
            raise ValueError("draft_saved_path requires a materialized draft")
        if self.draft is not None:
            # Defense in depth: the reused DraftAssetEntry validator already
            # forces these, but assert the draft-only contract at the boundary.
            if self.draft.asset_lifecycle_status is not AssetLifecycleStatus.DRAFT:
                raise ValueError("gate output draft must be lifecycle DRAFT")
            if self.draft.release_allowed or self.draft.runtime_catalog_registered:
                raise ValueError("gate output draft must not be released/registered")
        return self


class FallbackGateError(RuntimeError):
    """Raised on caller misuse (e.g. no compile path available)."""


class FallbackVerificationGate:
    """Compose deterministic checks into one blank/unsafe/unprintable gate.

    Generator-agnostic pure predicate (D1): input is ``(scad_source, mesh)`` via
    seams, never an agent call.  Construction injects the seams and policies;
    ``evaluate`` runs the checks and returns a verdict + draft-only output.
    """

    def __init__(
        self,
        *,
        compile_scad: CompileScadSeam | None = None,
        mesh_validator: MeshSoundnessProbe | None = None,
        complexity_limits: ComplexityLimits | None = None,
        printability_policy: PrintabilityGuardPolicy | None = None,
        draft_queue: DraftReviewQueue | None = None,
    ) -> None:
        self._compile_scad = compile_scad
        self._mesh_validator = mesh_validator
        self._complexity_limits = complexity_limits
        self._printability_policy = printability_policy
        self._draft_queue = draft_queue

    def evaluate(self, request: FallbackGateRequest) -> FallbackGateOutcome:
        reasons: list[str] = []

        source_presence = self._check_source_presence(request.scad_source, reasons)
        sandbox_tokens = self._check_sandbox_tokens(request.scad_source, reasons)
        static_complexity = self._check_static_complexity(request.scad_source, reasons)

        compilation, stl_path = self._compile(request, reasons)

        if stl_path is None:
            l2_geometry = _not_checked("l2_geometry", "no compiled mesh available")
            printability = _not_checked("printability", "no compiled mesh available")
            watertight_checked = False
            watertight_passed: bool | None = None
        else:
            l2_geometry, l2_report = self._check_l2_geometry(stl_path, reasons)
            printability = self._check_printability(l2_report, reasons)
            watertight_checked, watertight_passed = self._check_watertight(
                stl_path, reasons
            )

        passed = not reasons
        verdict = FallbackGateVerdict(
            automated_gate_passed=passed,
            source_presence=source_presence,
            sandbox_tokens=sandbox_tokens,
            static_complexity=static_complexity,
            compilation=compilation,
            l2_geometry=l2_geometry,
            printability=printability,
            watertight_checked=watertight_checked,
            watertight_passed=watertight_passed,
            blocking_reasons=tuple(reasons),
            gate_gaps=(OVERHANG_GATE_GAP,),
        )

        if not passed or request.draft_descriptor is None:
            return FallbackGateOutcome(verdict=verdict)

        draft = _build_draft_only(request.draft_descriptor)
        saved_path: Path | None = None
        if self._draft_queue is not None:
            saved_path = self._draft_queue.save_draft(draft)
        return FallbackGateOutcome(
            verdict=verdict, draft=draft, draft_saved_path=saved_path
        )

    # -- individual checks ------------------------------------------------

    def _check_source_presence(
        self, scad_source: str, reasons: list[str]
    ) -> GateCheckResult:
        if not scad_source.strip():
            reason = "blank_source: SCAD source is empty or whitespace-only"
            reasons.append(reason)
            return GateCheckResult(
                name="source_presence",
                status=FallbackGateCheckStatus.BLOCKED,
                detail=reason,
            )
        return GateCheckResult(
            name="source_presence", status=FallbackGateCheckStatus.PASSED
        )

    def _check_sandbox_tokens(
        self, scad_source: str, reasons: list[str]
    ) -> GateCheckResult:
        if _EXTERNAL_FILE_ACCESS_RE.search(scad_source):
            reason = (
                "sandbox_tokens: forbidden OpenSCAD external-file directive "
                "(include/use/import/surface)"
            )
            reasons.append(reason)
            return GateCheckResult(
                name="sandbox_tokens",
                status=FallbackGateCheckStatus.BLOCKED,
                detail=reason,
            )
        return GateCheckResult(
            name="sandbox_tokens", status=FallbackGateCheckStatus.PASSED
        )

    def _check_static_complexity(
        self, scad_source: str, reasons: list[str]
    ) -> GateCheckResult:
        verdict = check_scad_complexity(scad_source, self._complexity_limits)
        if not verdict.ok:
            reason = f"static_complexity: {verdict.reason_code} ({'; '.join(verdict.violations)})"
            reasons.append(reason)
            return GateCheckResult(
                name="static_complexity",
                status=FallbackGateCheckStatus.BLOCKED,
                detail=reason,
            )
        return GateCheckResult(
            name="static_complexity", status=FallbackGateCheckStatus.PASSED
        )

    def _compile(
        self, request: FallbackGateRequest, reasons: list[str]
    ) -> tuple[GateCheckResult, Path | None]:
        if request.precompiled_stl_path is not None:
            stl_path = request.precompiled_stl_path
            if not stl_path.is_file():
                reason = f"compilation: precompiled STL is missing or not a file: {stl_path}"
                reasons.append(reason)
                return (
                    GateCheckResult(
                        name="compilation",
                        status=FallbackGateCheckStatus.BLOCKED,
                        detail=reason,
                    ),
                    None,
                )
            return (
                GateCheckResult(
                    name="compilation",
                    status=FallbackGateCheckStatus.PASSED,
                    detail="precompiled",
                ),
                stl_path,
            )

        if self._compile_scad is None:
            raise FallbackGateError(
                "no compile path available: provide request.precompiled_stl_path "
                "or construct the gate with a compile_scad seam"
            )

        # Reached only when no precompiled path was supplied (handled above);
        # the seam decides where it writes, workdir is just a hint.
        workdir = Path(".")
        try:
            stl_path = self._compile_scad(request.scad_source, workdir)
        except Exception as exc:  # seam failure is a deterministic BLOCK, not a crash
            reason = f"compilation: compile seam failed: {exc}"
            reasons.append(reason)
            return (
                GateCheckResult(
                    name="compilation",
                    status=FallbackGateCheckStatus.BLOCKED,
                    detail=reason,
                ),
                None,
            )
        if not stl_path.is_file():
            reason = f"compilation: compile seam produced no STL at {stl_path}"
            reasons.append(reason)
            return (
                GateCheckResult(
                    name="compilation",
                    status=FallbackGateCheckStatus.BLOCKED,
                    detail=reason,
                ),
                None,
            )
        return (
            GateCheckResult(
                name="compilation", status=FallbackGateCheckStatus.PASSED
            ),
            stl_path,
        )

    def _check_l2_geometry(
        self, stl_path: Path, reasons: list[str]
    ) -> tuple[GateCheckResult, L2GeometryReport]:
        # spec=None, rendered=None => pure-mesh first-pass only
        # (extents/volume/triangle/vertex); assembly-only fields stay inactive.
        l2_report = evaluate_assembly_stl(stl_path, spec=None, rendered=None)
        if not l2_report.passed:
            reason = f"l2_geometry: geometry presence smoke failed ({dict(l2_report.metadata)})"
            reasons.append(reason)
            return (
                GateCheckResult(
                    name="l2_geometry",
                    status=FallbackGateCheckStatus.BLOCKED,
                    detail=reason,
                ),
                l2_report,
            )
        return (
            GateCheckResult(
                name="l2_geometry", status=FallbackGateCheckStatus.PASSED
            ),
            l2_report,
        )

    def _check_printability(
        self, l2_report: L2GeometryReport, reasons: list[str]
    ) -> GateCheckResult:
        # Serial dependency: feed the same L2 report (no mesh re-parse).
        report = evaluate_printability(l2_report, policy=self._printability_policy)
        if report.status is PrintabilityStatus.SMOKE_PASSED:
            return GateCheckResult(
                name="printability", status=FallbackGateCheckStatus.PASSED
            )
        reason = f"printability: not slice-clean ({report.status.value})"
        reasons.append(reason)
        return GateCheckResult(
            name="printability",
            status=FallbackGateCheckStatus.BLOCKED,
            detail=reason,
        )

    def _check_watertight(
        self, stl_path: Path, reasons: list[str]
    ) -> tuple[bool, bool | None]:
        if self._mesh_validator is None:
            # No seam => watertight unverified; never a PASS basis (R12).
            return False, None
        result = self._mesh_validator(stl_path)
        if not result.watertight:
            reasons.append(
                f"watertight: mesh_validator seam reports non-watertight ({result.detail})"
            )
            return True, False
        if result.wall_thickness_ok is False:
            reasons.append(
                f"watertight: mesh_validator seam reports thin walls ({result.detail})"
            )
            return True, False
        return True, True


def _not_checked(name: str, detail: str) -> GateCheckResult:
    return GateCheckResult(
        name=name, status=FallbackGateCheckStatus.NOT_CHECKED, detail=detail
    )


def _build_draft_only(descriptor: FallbackDraftDescriptor) -> DraftAssetEntry:
    """Materialize a draft-only entry; all lifecycle fields forced closed.

    The reused ``DraftAssetEntry`` validator (``_draft_starts_closed``) rejects
    any non-closed state, so curated/release is unrepresentable here (D4).
    """

    return DraftAssetEntry(
        draft_asset_id=descriptor.draft_asset_id,
        asset_lifecycle_status=AssetLifecycleStatus.DRAFT,
        subject=descriptor.subject,
        category=descriptor.category,
        style=descriptor.style,
        source_type=descriptor.source_type,
        license=descriptor.license,
        source_url_or_owner=descriptor.source_url_or_owner,
        provenance_note=descriptor.provenance_note,
        originating_prompt=descriptor.originating_prompt,
        draft_generator=descriptor.draft_generator,
        draft_renderer_version=descriptor.draft_renderer_version,
        review_queue_reason=descriptor.review_queue_reason,
        default_size_mm=descriptor.default_size_mm,
        min_size_mm=descriptor.min_size_mm,
        recommended_thickness_mm=descriptor.recommended_thickness_mm,
        required_features=descriptor.required_features,
        # lifecycle fields below are left at their closed defaults:
        #   visual_quality_status=review_required, legal=not_reviewed,
        #   printability=review_required, release_allowed=False,
        #   runtime_catalog_registered=False.
    )
