"""Prompt-based CAD coder agent backed by a ``BaseLLMProvider`` (Phase 8B).

Translates a mechanical subtask description into a self-contained CAD
script in the requested DSL by calling a real LLM provider. The agent is
deliberately thin:

- It owns prompt construction per DSL (system prompt + user prompt).
- It owns code extraction (LLM may wrap the answer in a fenced block).
- It validates that the extracted snippet is non-empty and references the
  expected entrypoint name.
- All transport/key/model concerns live in the injected provider so this
  agent works with any provider that satisfies ``BaseLLMProvider``.

Fail-fast semantics (DESIGN.md R10):
- Any LLM failure surfaces as ``CADCoderAgentError`` with the upstream
  exception chained — no silent mock fallback.
- An empty or fence-less response that yields no code raises
  ``CADCoderAgentError`` so downstream sandboxes never run a blank script.
"""

from __future__ import annotations

import ast
import re

from modules.agents.cad_coder.base import BaseCADCoderAgent
from modules.agents.cad_coder.exceptions import CADCoderAgentError, CADCoderErrorCode
from modules.agents.cad_coder.schemas import CADCoderRequest, CADCoderResponse, CADDsl
from modules.llm.base import BaseLLMProvider
from modules.llm.exceptions import LLMProviderError
from modules.llm.schemas import LLMMessage, LLMRequest, LLMResponse
from modules.requirements.schemas import RequirementSpec

__all__ = ["PromptBasedCADCoderAgent"]

_DEFAULT_ENTRYPOINT = "main"
_OPENSCAD_MAX_SIGNIFICANT_LINES = 180

_DSL_GUIDANCE: dict[CADDsl, str] = {
    "cadquery": (
        "Write a single self-contained Python script using the `cadquery` library. "
        "Define a function `main()` that returns the final `cadquery.Workplane` "
        "or `cadquery.Assembly`. Do not call `main()` at module scope. "
        "Do not write to disk. Only use cadquery primitives and standard library."
    ),
    "build123d": (
        "Write a single self-contained Python script using the `build123d` library. "
        "Define a function `main()` that returns the final `build123d` part / sketch. "
        "Do not call `main()` at module scope. Do not write to disk."
    ),
    "openscad": (
        "Target DSL: OpenSCAD. Write only one complete, self-contained "
        "OpenSCAD script in millimeters. Do not return prose, Markdown, "
        "comments that explain the answer, or multiple alternatives. Fit the "
        "model inside the requested bounding box when dimensions are provided. "
        "Keep the model printable as one connected solid part with no floating "
        "separate pieces and avoid zero-thickness surfaces. Prefer a flat, "
        "support-light 3D-printable mechanical structure whose lowest geometry "
        "starts at Z=0. Use minimum wall thickness of at least 2.5 mm. "
        "If the request says a size is exact, fixed, or must measure a given "
        "X/Y/Z dimension, make the final outer extents match that dimension "
        "within about 1 mm; do not reinterpret it as merely a maximum size. "
        "For drone frames or other radial parts, account for motor mount "
        "radius, arm angle, and embossed geometry so the final bounding box "
        "still matches the requested fixed dimensions. "
        "`module main()` and call `main();` exactly once at the bottom. The "
        "final non-comment line must be exactly `main();`. Do not use "
        "include, use, import, surface, or any external file dependency. "
        "Use difference() for real through-holes. If adding embossed text, "
        "make the raised text physically touch the body. Keep the script "
        f"concise, complete, and no longer than {_OPENSCAD_MAX_SIGNIFICANT_LINES} "
        "significant lines. Prefer "
        "simple OpenSCAD primitives such as union, difference, translate, "
        "rotate, cube, cylinder, linear_extrude, and text. "
        "OpenSCAD geometry calls are statements, not assignable values: do not "
        "write patterns like `part = difference() { ... };`, "
        "`shape = union() { ... };`, or `x = translate(...) { ... };`. Put "
        "geometry inside modules and call those modules as statements. "
        "When the user asks for embossed lettering or a logo, model it as raised "
        "geometry on the top surface rather than as a comment or annotation. "
        "If the requested design is complex, prioritize a complete shorter "
        "manufacturable script over exhaustive visual detail."
    ),
}

_CODE_FENCE_RE = re.compile(
    r"```([a-zA-Z0-9_+\-]*)\s*\n(.*?)```",
    re.DOTALL,
)
_PYTHON_FENCE_LANGS = frozenset({"python", "py"})
_OPENSCAD_FENCE_LANGS = frozenset({"openscad", "scad"})
_OPENSCAD_FORBIDDEN_RE = re.compile(
    r"(?i)(?:^\s*(?:include|use)\s*<|(?:^|[^\w])(?:import|surface)\s*\()",
    re.MULTILINE,
)
_OPENSCAD_GEOMETRY_ASSIGNMENT_RE = re.compile(
    r"(?im)^\s*[a-z_]\w*\s*=\s*"
    r"(?:union|difference|intersection|translate|rotate|scale|mirror|"
    r"color|hull|minkowski|linear_extrude|rotate_extrude|cube|cylinder|"
    r"sphere|polyhedron|text)\s*\(",
)
_TOKEN_LIMIT_FINISH_REASONS = frozenset(
    {
        "length",
        "max_tokens",
        "max_output_tokens",
        "max_tokens_reached",
        "max_output_tokens_reached",
    }
)


class PromptBasedCADCoderAgent(BaseCADCoderAgent):
    """LLM-driven CAD coder.

    Preconditions:
        provider is fully configured (model pinned, credentials present).
    Raises:
        CADCoderAgentError if the provider fails or the response contains
        no usable script body.
    """

    default_adapter_name = "prompt-based-cad-coder-v1"

    def __init__(
        self,
        provider: BaseLLMProvider,
        *,
        adapter_label: str | None = None,
        max_tokens: int = 8192,
        temperature: float = 0.2,
        max_repair_attempts: int = 1,
    ) -> None:
        self._provider = provider
        self._adapter_label = adapter_label or self.default_adapter_name
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._max_repair_attempts = max_repair_attempts

    @property
    def adapter_name(self) -> str:
        return self._adapter_label

    @property
    def provider_name(self) -> str:
        return self._provider.provider_name

    async def generate_code(self, request: CADCoderRequest) -> CADCoderResponse:
        last_error: CADCoderAgentError | None = None
        seen_error_signatures: set[tuple[CADCoderErrorCode, str | None]] = set()
        llm_request = _initial_llm_request(
            request,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )

        for attempt in range(self._max_repair_attempts + 1):
            try:
                response = await self._provider.complete(llm_request)
            except LLMProviderError as exc:
                raise CADCoderAgentError(
                    f"LLM provider '{self._provider.provider_name}' failed for "
                    f"subtask '{request.subtask_id}': {exc}",
                    code=_provider_error_code(exc),
                    provider=self._provider.provider_name,
                    model=_provider_model(self._provider, llm_request),
                    timeout_s=_provider_timeout_s(self._provider),
                    retry_count=attempt,
                    repair_attempts=attempt,
                    metadata={
                        "subtask_id": request.subtask_id,
                        "provider_error_type": type(exc).__name__,
                    },
                ) from exc

            try:
                code = _normalize_and_validate_response(
                    response,
                    dsl=request.dsl,
                    description=request.description,
                    requirements=request.requirements,
                    provider_name=self._provider.provider_name,
                    subtask_id=request.subtask_id,
                )
            except CADCoderAgentError as exc:
                enriched = _with_attempt_context(
                    exc,
                    provider=self._provider.provider_name,
                    model=response.model,
                    subtask_id=request.subtask_id,
                    attempt=attempt,
                )
                last_error = enriched
                error_signature = _error_signature(enriched)
                if error_signature is not None and error_signature in seen_error_signatures:
                    repeated_metadata = dict(enriched.metadata)
                    repeated_metadata.update(
                        {
                            "repeated": True,
                            "bounded_repair": True,
                            "max_repair_attempts": self._max_repair_attempts,
                            "repeated_error_signature": list(error_signature),
                        }
                    )
                    raise CADCoderAgentError(
                        "Repeated CAD coder contract failure "
                        f"({error_signature[0].value}); refusing further repair. "
                        f"{enriched}",
                        code=enriched.code,
                        provider=self._provider.provider_name,
                        model=response.model,
                        retry_count=attempt,
                        repair_attempts=attempt,
                        fallback_recommended="template",
                        metadata=repeated_metadata,
                    ) from exc
                if error_signature is not None:
                    seen_error_signatures.add(error_signature)
                if attempt >= self._max_repair_attempts:
                    raise enriched from exc
                llm_request = _repair_llm_request(
                    original=request,
                    failed_content=response.content,
                    error_detail=str(enriched),
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                )
                continue

            return CADCoderResponse(
                subtask_id=request.subtask_id,
                dsl=request.dsl,
                code=code,
                entrypoint=_DEFAULT_ENTRYPOINT,
                trace_id=request.trace_id,
            )

        raise last_error or CADCoderAgentError(
            f"LLM provider '{self._provider.provider_name}' did not return "
            f"usable CAD code for subtask '{request.subtask_id}'."
        )


def _provider_error_code(exc: LLMProviderError) -> CADCoderErrorCode:
    message = str(exc).lower()
    if "empty response" in message or "empty content" in message:
        return CADCoderErrorCode.EMPTY_RESPONSE
    return CADCoderErrorCode.SYNTAX_CONTRACT_FAILED


def _provider_model(provider: BaseLLMProvider, request: LLMRequest) -> str | None:
    if request.model:
        return request.model
    value = getattr(provider, "_model", None)
    return value if isinstance(value, str) and value else None


def _provider_timeout_s(provider: BaseLLMProvider) -> float | None:
    value = getattr(provider, "_timeout_s", None)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _with_attempt_context(
    exc: CADCoderAgentError,
    *,
    provider: str,
    model: str,
    subtask_id: str,
    attempt: int,
) -> CADCoderAgentError:
    metadata = dict(exc.metadata)
    metadata.setdefault("subtask_id", subtask_id)
    return CADCoderAgentError(
        str(exc),
        code=exc.code or CADCoderErrorCode.SYNTAX_CONTRACT_FAILED,
        provider=provider,
        model=model,
        retry_count=attempt,
        repair_attempts=attempt,
        fallback_recommended=exc.fallback_recommended,
        metadata=metadata,
    )


def _error_signature(
    exc: CADCoderAgentError,
) -> tuple[CADCoderErrorCode, str | None] | None:
    if exc.code is None:
        return None
    entrypoint_failure_kind = exc.metadata.get("entrypoint_failure_kind")
    if not isinstance(entrypoint_failure_kind, str):
        entrypoint_failure_kind = None
    return (exc.code, entrypoint_failure_kind)


def _initial_llm_request(
    request: CADCoderRequest,
    *,
    temperature: float,
    max_tokens: int,
) -> LLMRequest:
    return LLMRequest(
        messages=(
            LLMMessage(role="system", content=_system_prompt(request.dsl)),
            LLMMessage(role="user", content=_user_prompt(request)),
        ),
        temperature=temperature,
        max_tokens=max_tokens,
        trace_id=request.trace_id,
    )


def _repair_llm_request(
    *,
    original: CADCoderRequest,
    failed_content: str,
    error_detail: str,
    temperature: float,
    max_tokens: int,
) -> LLMRequest:
    return LLMRequest(
        messages=(
            LLMMessage(role="system", content=_system_prompt(original.dsl)),
            LLMMessage(
                role="user",
                content=_repair_prompt(
                    original=original,
                    failed_content=failed_content,
                    error_detail=error_detail,
                ),
            ),
        ),
        temperature=temperature,
        max_tokens=max_tokens,
        trace_id=original.trace_id,
    )


def _system_prompt(dsl: CADDsl) -> str:
    guidance = _DSL_GUIDANCE[dsl]
    output_contract = (
        "Respond with ONLY raw script text. Do not wrap the answer in Markdown "
        "or a code fence. If a provider adds a fence anyway, it must be a single "
        "matching code block for the requested DSL and contain no prose outside it."
    )
    return (
        "You are a senior mechanical CAD engineer that writes deterministic, "
        "self-contained CAD scripts. " + guidance + " "
        + output_contract
    )


def _user_prompt(request: CADCoderRequest) -> str:
    lines = [
        f"Subtask id: {request.subtask_id}",
        f"Target DSL: {request.dsl}",
        f"Required entrypoint: {_DEFAULT_ENTRYPOINT}",
        "",
        "Description:",
        request.description.strip(),
    ]
    if request.constraints:
        lines.extend(["", "Constraints:"])
        for key, value in sorted(request.constraints.items()):
            lines.append(f"- {key}: {value}")
    if request.requirements is not None:
        lines.extend(["", "Structured requirements:"])
        lines.extend(_format_requirement_spec(request))
    if request.dsl == "openscad":
        brief = _openscad_hidden_design_brief(request.description)
        if brief:
            lines.extend(["", "Internal manufacturing brief:", *brief])
    return "\n".join(lines)


def _openscad_hidden_design_brief(description: str) -> list[str]:
    lowered = description.lower()
    if _is_decorative_keyring_request(lowered):
        brief = [
            "- Interpret this as a flat 2.5D decorative keyring/charm that the "
            "LLM must model directly in OpenSCAD, not as a template lookup.",
            "- The result must be one connected printable part with a real "
            "through-hole for the metal ring and at least 2.5 mm material "
            "around that hole.",
            "- Avoid a blank tag. Preserve the requested mascot/character as "
            "visible raised or cut geometry on the body.",
            "- Use short descriptive module, variable, or comment names for "
            "visible features such as head, ears, face, eyes, nose, mouth, "
            "paws, body, and keyring hole so semantic validation can catch "
            "empty designs before slicing.",
            "- Prefer simple circles, hulls, extruded text, and raised/shallow "
            "relief details that are robust for FDM printing.",
        ]
        if _contains_bear_intent(lowered):
            brief.extend(
                [
                    "- For a bear or teddy-bear keyring, include a recognizable "
                    "rounded head, two ears, face details, eyes, nose/muzzle, "
                    "and body or paw detail. A plain loop or blank tag is a "
                    "failed design.",
                    "- Keep all facial and decorative details physically "
                    "touching the base so the STL remains one connected part.",
                ]
            )
        return brief
    if any(marker in lowered for marker in ("drone", "quadcopter", "드론", "쿼드")):
        return [
            "- Interpret the request as a 200 mm-class quadcopter frame unless a "
            "different explicit size is present.",
            "- Use approximately 200 mm X/Y outer span, 5 mm thickness, central hub, "
            "four X-layout arms, four motor mounts, through-holes in each motor "
            "mount, central wiring/lightening holes, and a raised Generic Printer logo.",
            "- Keep the result a single flat printable part that does not require "
            "excessive support.",
            "- Avoid a plain cross-bar silhouette; make motor-mount regions look "
            "structurally reinforced while keeping the script short.",
        ]
    if any(marker in lowered for marker in ("bracket", "브라켓")):
        return [
            "- Interpret this as a simple flat printable mounting bracket unless "
            "the user asks for a different form.",
            "- Use visible real geometry for a bracket_body and mounting_holes. "
            "Prefer module names such as bracket_body() and mounting_hole() so "
            "semantic validation can trace the generated features.",
            "- If a hole count is requested, create that many through-holes with "
            "difference(); do not represent holes as labels or comments.",
            "- Keep the part a single connected solid with practical wall "
            "thickness around the holes.",
        ]
    if any(marker in lowered for marker in ("cup", "컵")):
        return [
            "- Interpret this as a simple printable cup or open container.",
            "- Use visible real geometry for a cup_body. Prefer a helper module "
            "named cup_body() so semantic validation can trace the generated "
            "feature.",
            "- Model the cup with a closed bottom and open top using difference(); "
            "do not represent the opening as text or comments.",
            "- Keep wall thickness practical for FDM printing and keep the part a "
            "single connected solid.",
        ]
    return []


def _format_requirement_spec(request: CADCoderRequest) -> list[str]:
    spec = request.requirements
    if spec is None:
        return []
    lines = [
        f"- object_type: {spec.object_type or 'unknown'}",
    ]
    for label, values in (
        ("intent_tags", spec.intent_tags),
        ("required_features", spec.required_features),
        ("forbidden_outcomes", spec.forbidden_outcomes),
        ("print_constraints", spec.print_constraints),
    ):
        if values:
            lines.append(f"- {label}: {', '.join(values)}")
    if spec.dimensions:
        lines.append("- dimensions:")
        for dimension in spec.dimensions:
            tolerance = (
                f", tolerance_mm={dimension.tolerance_mm}"
                if dimension.tolerance_mm is not None
                else ""
            )
            lines.append(
                "  - "
                f"{dimension.name}: {dimension.value_mm:g} mm "
                f"({dimension.mode}{tolerance})"
            )
    if spec.quality_checks:
        checks = ", ".join(check.name for check in spec.quality_checks)
        lines.append(f"- quality_checks: {checks}")
    lines.append(
        "- Requirement contract: model these features directly in the CAD script; "
        "do not satisfy them with prose or comments only."
    )
    return lines


def _repair_prompt(
    *,
    original: CADCoderRequest,
    failed_content: str,
    error_detail: str,
) -> str:
    snippet = failed_content.strip()
    if len(snippet) > 4_000:
        snippet = snippet[:4_000] + "\n...<truncated failed response>..."
    return "\n".join(
        [
            f"Subtask id: {original.subtask_id}",
            f"Target DSL: {original.dsl}",
            f"Required entrypoint: {_DEFAULT_ENTRYPOINT}",
            "",
            "The previous OpenSCAD failed before STL export."
            if original.dsl == "openscad"
            else "The previous CAD script was rejected before execution.",
            f"Failure: {error_detail}",
            "",
            "Original description:",
            original.description.strip(),
            "",
            "Failed response excerpt:",
            snippet,
            "",
            "Rewrite the complete script from scratch.",
            "Preserve the design intent.",
            "Keep it shorter and simpler.",
            "Return complete OpenSCAD only."
            if original.dsl == "openscad"
            else "Return one complete script only.",
            "For OpenSCAD, it must define module main() and end with main();.",
            "For OpenSCAD, do not use include/use/import/surface or external files.",
            "For OpenSCAD, do not assign geometry calls such as union(), "
            "difference(), translate(), cube(), or cylinder() to variables.",
            "No prose, no Markdown fence unless the provider forces one single "
            "openscad/scad block.",
        ]
    )


def _normalize_and_validate_response(
    response: LLMResponse,
    *,
    dsl: CADDsl,
    description: str,
    requirements: RequirementSpec | None,
    provider_name: str,
    subtask_id: str,
) -> str:
    _validate_finish_reason(response.finish_reason, provider_name=provider_name)
    code = _extract_code(response.content, dsl=dsl)
    if not code.strip():
        raise CADCoderAgentError(
            f"LLM provider '{provider_name}' returned an empty CAD script for "
            f"subtask '{subtask_id}'.",
            code=CADCoderErrorCode.EMPTY_RESPONSE,
            provider=provider_name,
            metadata={"subtask_id": subtask_id},
        )
    _validate_completeness(code, dsl=dsl)
    _validate_entrypoint(code, dsl=dsl)
    _validate_semantic_intent(
        code,
        dsl=dsl,
        description=description,
        requirements=requirements,
    )
    return code


def _validate_finish_reason(reason: str | None, *, provider_name: str) -> None:
    if reason is None:
        return
    normalized = _normalize_finish_reason(reason)
    if normalized in _TOKEN_LIMIT_FINISH_REASONS:
        raise CADCoderAgentError(
            f"LLM provider '{provider_name}' stopped because it reached the "
            f"output token limit (finish_reason={reason!r}); refusing to run "
            "a potentially incomplete CAD script.",
            code=CADCoderErrorCode.SYNTAX_CONTRACT_FAILED,
            provider=provider_name,
            metadata={"finish_reason": reason},
        )


def _normalize_finish_reason(reason: str) -> str:
    lowered = reason.strip().lower()
    return re.sub(r"[\s\-]+", "_", lowered)


def _extract_code(content: str, *, dsl: CADDsl) -> str:
    fence_count = content.count("```")
    if fence_count % 2:
        _reject_unclosed_fence(content, dsl=dsl)
    matches = list(_CODE_FENCE_RE.finditer(content))
    if len(matches) > 1:
        raise CADCoderAgentError(
            "LLM returned multiple fenced code blocks; refusing to choose one "
            "for CAD execution.",
            code=CADCoderErrorCode.SYNTAX_CONTRACT_FAILED,
        )
    match = matches[0] if matches else None
    if match:
        lang = match.group(1).strip().lower()
        if lang and not _fence_language_matches(lang, dsl):
            raise CADCoderAgentError(
                f"LLM returned a fenced {lang!r} block for requested DSL "
                f"{dsl!r}; refusing to pass mismatched code downstream.",
                code=CADCoderErrorCode.SYNTAX_CONTRACT_FAILED,
            )
        if dsl == "openscad" and not lang:
            raise CADCoderAgentError(
                "LLM returned an unlabeled fenced block for OpenSCAD; only "
                "openscad/scad fences are allowed.",
                code=CADCoderErrorCode.SYNTAX_CONTRACT_FAILED,
            )
        return match.group(2).rstrip() + "\n"
    # Permit fence-less responses only when they look like a script:
    # require at least one DSL-relevant marker so we never return prose.
    stripped = content.strip()
    if _looks_like_script(stripped, dsl):
        return stripped + "\n"
    return ""


def _reject_unclosed_fence(content: str, *, dsl: CADDsl) -> None:
    lines = content.strip().splitlines()
    if not lines:
        return
    first = lines[0].strip()
    if not first.startswith("```"):
        return
    lang = first.removeprefix("```").strip().lower()
    if lang and not _fence_language_matches(lang, dsl):
        raise CADCoderAgentError(
            f"LLM returned an unclosed fenced {lang!r} block for requested DSL "
            f"{dsl!r}; refusing to pass mismatched code downstream.",
            code=CADCoderErrorCode.SYNTAX_CONTRACT_FAILED,
        )
    raise CADCoderAgentError(
        "LLM returned an unclosed fenced code block; refusing to run a "
        "potentially truncated CAD script.",
        code=CADCoderErrorCode.SYNTAX_CONTRACT_FAILED,
    )


def _looks_like_script(text: str, dsl: CADDsl) -> bool:
    if not text:
        return False
    if dsl == "openscad":
        return "module " in text or "cube(" in text or "sphere(" in text
    return "def " in text or "import " in text


def _fence_language_matches(lang: str, dsl: CADDsl) -> bool:
    if dsl in {"cadquery", "build123d"}:
        return lang in _PYTHON_FENCE_LANGS
    return lang in _OPENSCAD_FENCE_LANGS


def _validate_completeness(code: str, *, dsl: CADDsl) -> None:
    if dsl != "openscad":
        return
    _validate_forbidden_openscad_directives(code)
    _validate_no_openscad_geometry_assignment(code)
    _validate_openscad_line_budget(code)
    if not _openscad_braces_are_balanced(code):
        raise CADCoderAgentError(
            "OpenSCAD script has unbalanced braces; refusing to run a "
            "potentially incomplete CAD script.",
            code=CADCoderErrorCode.BRACE_UNBALANCED,
        )


def _validate_forbidden_openscad_directives(code: str) -> None:
    stripped = _strip_openscad_comments_and_strings(code)
    if _OPENSCAD_FORBIDDEN_RE.search(stripped):
        raise CADCoderAgentError(
            "OpenSCAD script must not use include/use/import/surface or external files.",
            code=CADCoderErrorCode.SYNTAX_CONTRACT_FAILED,
        )


def _validate_no_openscad_geometry_assignment(code: str) -> None:
    stripped = _strip_openscad_comments_and_strings(code)
    if _OPENSCAD_GEOMETRY_ASSIGNMENT_RE.search(stripped):
        raise CADCoderAgentError(
            "OpenSCAD geometry calls cannot be assigned to variables; emit "
            "geometry as statements inside module definitions instead.",
            code=CADCoderErrorCode.OPENSCAD_GEOMETRY_ASSIGNMENT,
        )


def _validate_openscad_line_budget(code: str) -> None:
    significant_lines = [
        line
        for line in code.splitlines()
        if line.strip() and not line.strip().startswith("//")
    ]
    if len(significant_lines) > _OPENSCAD_MAX_SIGNIFICANT_LINES:
        raise CADCoderAgentError(
            "OpenSCAD script is too long for the freeform CAD contract "
            f"({len(significant_lines)} significant lines > "
            f"{_OPENSCAD_MAX_SIGNIFICANT_LINES}); rewrite it as a shorter, "
            "simpler complete script.",
            code=CADCoderErrorCode.SYNTAX_CONTRACT_FAILED,
        )


def _openscad_braces_are_balanced(code: str) -> bool:
    depth = 0
    for char in _strip_openscad_comments_and_strings(code):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _strip_openscad_comments_and_strings(code: str) -> str:
    result: list[str] = []
    i = 0
    in_string = False
    while i < len(code):
        char = code[i]
        nxt = code[i + 1] if i + 1 < len(code) else ""

        if in_string:
            if char == "\\":
                i += 2
                continue
            if char == '"':
                in_string = False
            i += 1
            continue

        if char == '"':
            in_string = True
            i += 1
            continue
        if char == "/" and nxt == "/":
            i = code.find("\n", i)
            if i == -1:
                break
            result.append("\n")
            i += 1
            continue
        if char == "/" and nxt == "*":
            end = code.find("*/", i + 2)
            if end == -1:
                break
            result.append("\n" * code[i:end + 2].count("\n"))
            i = end + 2
            continue

        result.append(char)
        i += 1
    return "".join(result)


def _validate_entrypoint(code: str, *, dsl: CADDsl) -> None:
    if dsl == "openscad":
        stripped = _strip_openscad_comments_and_strings(code)
        if not re.search(r"\bmodule\s+main\s*\(", stripped):
            raise CADCoderAgentError(
                "OpenSCAD script must define module main().",
                code=CADCoderErrorCode.MISSING_MAIN_CALL,
                metadata={"entrypoint_failure_kind": "missing_module"},
            )
        call_count = len(_openscad_main_calls(stripped))
        if call_count != 1:
            raise CADCoderAgentError(
                "OpenSCAD script must call main(); exactly once.",
                code=CADCoderErrorCode.MISSING_MAIN_CALL,
                metadata={"entrypoint_failure_kind": "missing_call"},
            )
        if _last_significant_line(stripped) != "main();":
            raise CADCoderAgentError(
                "OpenSCAD script must end with main(); as the final statement.",
                code=CADCoderErrorCode.MISSING_MAIN_CALL,
                metadata={"entrypoint_failure_kind": "final_statement"},
            )
        return

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise CADCoderAgentError(
            f"Python CAD script failed syntax validation: {exc.msg}.",
            code=CADCoderErrorCode.SYNTAX_CONTRACT_FAILED,
        ) from exc

    has_main = any(
        isinstance(node, ast.FunctionDef) and node.name == _DEFAULT_ENTRYPOINT
        for node in tree.body
    )
    if not has_main:
        raise CADCoderAgentError(
            f"Python CAD script must define function {_DEFAULT_ENTRYPOINT}().",
            code=CADCoderErrorCode.MISSING_MAIN_CALL,
        )


def _validate_semantic_intent(
    code: str,
    *,
    dsl: CADDsl,
    description: str,
    requirements: RequirementSpec | None,
) -> None:
    if dsl != "openscad":
        return
    lowered_description = description.lower()
    has_bear_keyring_requirement = bool(
        requirements
        and requirements.object_type == "keyring"
        and "bear" in requirements.intent_tags
    )
    if not has_bear_keyring_requirement and not (
        _is_decorative_keyring_request(lowered_description)
        and _contains_bear_intent(lowered_description)
    ):
        return

    stripped = _strip_openscad_comments_and_strings(code).lower()
    feature_markers = (
        "head",
        "ear",
        "ears",
        "face",
        "eye",
        "eyes",
        "nose",
        "muzzle",
        "mouth",
        "paw",
        "paws",
        "body",
        "belly",
        "cheek",
        "곰",
        "귀",
        "얼굴",
        "눈",
        "코",
        "입",
        "몸",
        "발",
    )
    matched = {marker for marker in feature_markers if marker in stripped}
    if len(matched) < 3:
        raise CADCoderAgentError(
            "OpenSCAD script does not preserve the bear-keyring design intent; "
            "it must contain visible named geometry for bear features such as "
            "head, ears, face, eyes, nose/muzzle, body, or paws. Refusing to "
            "slice a semantically blank keyring.",
            code=CADCoderErrorCode.SEMANTIC_FAILURE,
        )
    if "difference" not in stripped:
        raise CADCoderAgentError(
            "OpenSCAD keyring script must use difference() to create a real "
            "through-hole for the keyring.",
            code=CADCoderErrorCode.SEMANTIC_FAILURE,
        )


def _openscad_main_calls(code: str) -> list[str]:
    return re.findall(r"(?m)^\s*main\s*\(\s*\)\s*;\s*(?://.*)?$", code)


def _last_significant_line(code: str) -> str:
    for line in reversed(code.splitlines()):
        stripped = line.strip()
        if stripped and not stripped.startswith("//"):
            return stripped
    return ""


def _is_decorative_keyring_request(lowered_description: str) -> bool:
    keyring_markers = (
        "keyring",
        "key ring",
        "keychain",
        "key chain",
        "charm",
        "키링",
        "열쇠고리",
        "키체인",
    )
    return any(marker in lowered_description for marker in keyring_markers)


def _contains_bear_intent(lowered_description: str) -> bool:
    bear_markers = ("bear", "teddy", "teddy-bear", "곰", "곰돌", "베어")
    return any(marker in lowered_description for marker in bear_markers)
