"""LLM-backed prompt normalizer for template-driven CAD generation."""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from modules.llm.base import BaseLLMProvider
from modules.llm.exceptions import LLMProviderError
from modules.llm.schemas import LLMMessage, LLMRequest
from modules.parametric_cad.schemas import ManufacturingParams

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


class ParametricCADNormalizerError(RuntimeError):
    """Raised when a prompt cannot be normalized into safe CAD parameters."""


class ParametricCADNormalizer:
    """Convert a mechanical task description into validated CAD parameters."""

    default_adapter_name = "parametric-cad-normalizer-v1"

    def __init__(
        self,
        provider: BaseLLMProvider,
        *,
        max_tokens: int = 2048,
        temperature: float = 0.0,
        max_repair_attempts: int = 1,
    ) -> None:
        self._provider = provider
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._max_repair_attempts = max_repair_attempts

    async def normalize(
        self,
        *,
        description: str,
        prompt: str,
        trace_id: str | None = None,
    ) -> ManufacturingParams:
        llm_request = _initial_request(
            description=description,
            prompt=prompt,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            trace_id=trace_id,
        )
        last_error: Exception | None = None
        last_content = ""
        for attempt in range(self._max_repair_attempts + 1):
            try:
                response = await self._provider.complete(llm_request)
            except LLMProviderError as exc:
                raise ParametricCADNormalizerError(
                    f"LLM provider '{self._provider.provider_name}' failed while "
                    f"normalizing CAD parameters: {exc}"
                ) from exc
            last_content = response.content
            try:
                return ManufacturingParams.model_validate(
                    _extract_json_object(response.content)
                )
            except (json.JSONDecodeError, TypeError, ValueError, ValidationError) as exc:
                last_error = exc
                if attempt >= self._max_repair_attempts:
                    break
                llm_request = _repair_request(
                    description=description,
                    prompt=prompt,
                    failed_content=response.content,
                    error_detail=str(exc),
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                    trace_id=trace_id,
                )

        raise ParametricCADNormalizerError(
            "Prompt normalizer returned invalid ManufacturingParams JSON: "
            f"{last_error}. Response excerpt: {last_content[:500]!r}"
        )


def _initial_request(
    *,
    description: str,
    prompt: str,
    temperature: float,
    max_tokens: int,
    trace_id: str | None,
) -> LLMRequest:
    return LLMRequest(
        messages=(
            LLMMessage(role="system", content=_system_prompt()),
            LLMMessage(
                role="user",
                content=_user_prompt(description=description, prompt=prompt),
            ),
        ),
        temperature=temperature,
        max_tokens=max_tokens,
        trace_id=trace_id,
    )


def _repair_request(
    *,
    description: str,
    prompt: str,
    failed_content: str,
    error_detail: str,
    temperature: float,
    max_tokens: int,
    trace_id: str | None,
) -> LLMRequest:
    snippet = failed_content.strip()
    if len(snippet) > 2_000:
        snippet = snippet[:2_000] + "\n...<truncated failed response>..."
    return LLMRequest(
        messages=(
            LLMMessage(role="system", content=_system_prompt()),
            LLMMessage(
                role="user",
                content="\n".join(
                    [
                        "The previous JSON was rejected.",
                        f"Failure: {error_detail}",
                        "",
                        _user_prompt(description=description, prompt=prompt),
                        "",
                        "Rejected response excerpt:",
                        snippet,
                        "",
                        "Return one corrected JSON object only.",
                    ]
                ),
            ),
        ),
        temperature=temperature,
        max_tokens=max_tokens,
        trace_id=trace_id,
    )


def _system_prompt() -> str:
    return (
        "You normalize Korean user requests into strict manufacturing parameters "
        "for deterministic OpenSCAD templates. Return ONLY one JSON object. "
        "No markdown, no prose. Schema: "
        "{"
        '"part_type":"drone_frame|cup|bracket|pen_holder|fixture",'
        '"outer_size_mm":[x,y],'
        '"height_mm":number,'
        '"thickness_mm":number,'
        '"wall_thickness_mm":number,'
        '"exact_outer_size":boolean,'
        '"hole_count":integer,'
        '"hole_diameter_mm":number,'
        '"motor_mount_count":4,'
        '"motor_mount_diameter_mm":number,'
        '"logo":{"text":"","mode":"none|embossed|debossed",'
        '"placement":"top_surface","height_mm":number},'
        '"notes":""'
        "}. "
        "Preserve exact dimensions. If the user says 200 x 200 mm, outer_size_mm "
        "must be [200,200] and exact_outer_size must be true; do not shrink it to "
        "fit inside a bounding box. Use drone_frame for drone frames, cup for cups, "
        "pen_holder for pen holders, bracket for L brackets, and fixture for flat "
        "jigs, supports, plates, or ambiguous printer-side stands. Use embossed "
        "logo when the user asks for raised text or 양각."
    )


def _user_prompt(*, description: str, prompt: str) -> str:
    return "\n".join(
        [
            "Original user prompt:",
            prompt.strip(),
            "",
            "Mechanical subtask description:",
            description.strip(),
        ]
    )


def _extract_json_object(content: str) -> dict[str, object]:
    text = content.strip()
    match = _JSON_FENCE_RE.search(text)
    if match:
        text = match.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("response did not contain a JSON object")
    parsed = json.loads(_strip_json_line_comments(text[start : end + 1]))
    if not isinstance(parsed, dict):
        raise TypeError("normalizer response root must be a JSON object")
    return parsed


def _strip_json_line_comments(text: str) -> str:
    cleaned: list[str] = []
    for line in text.splitlines():
        in_string = False
        escaped = False
        cut_at = len(line)
        for index, char in enumerate(line):
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if not in_string and char == "/" and index + 1 < len(line):
                if line[index + 1] == "/":
                    cut_at = index
                    break
        cleaned.append(line[:cut_at])
    return "\n".join(cleaned)
