"""Prompt-based planner agent backed by a ``BaseLLMProvider``.

The planner turns a user prompt into the existing provider-neutral
``PlannerResponse`` schema. It deliberately owns only prompt construction and
JSON normalization; model/key/transport details stay in the injected LLM
provider, and the pydantic planner schema remains the final contract guard.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from modules.agents.planner.base import BasePlannerAgent
from modules.agents.planner.exceptions import PlannerAgentError
from modules.agents.planner.schemas import PlannerRequest, PlannerResponse
from modules.llm.base import BaseLLMProvider
from modules.llm.exceptions import LLMProviderError
from modules.llm.schemas import LLMMessage, LLMRequest

__all__ = ["PromptBasedPlannerAgent"]

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


class PromptBasedPlannerAgent(BasePlannerAgent):
    """LLM-driven planner using the canonical ``PlannerResponse`` contract."""

    default_adapter_name = "prompt-based-planner-v1"

    def __init__(
        self,
        provider: BaseLLMProvider,
        *,
        adapter_label: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
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

    async def plan(self, request: PlannerRequest) -> PlannerResponse:
        llm_request = _initial_llm_request(
            request,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )
        last_error: Exception | None = None

        for attempt in range(self._max_repair_attempts + 1):
            try:
                response = await self._provider.complete(llm_request)
            except LLMProviderError as exc:
                raise PlannerAgentError(
                    f"LLM provider '{self._provider.provider_name}' failed while "
                    f"planning session '{request.session_id}': {exc}"
                ) from exc

            try:
                payload = _extract_json_object(response.content)
                payload.setdefault("trace_id", request.trace_id)
                return PlannerResponse.model_validate(payload)
            except (
                json.JSONDecodeError,
                TypeError,
                ValueError,
                ValidationError,
            ) as exc:
                last_error = exc
                if attempt >= self._max_repair_attempts:
                    break
                llm_request = _repair_llm_request(
                    original=request,
                    failed_content=response.content,
                    error_detail=str(exc),
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                )

        raise PlannerAgentError(
            "Prompt-based planner returned an invalid PlannerResponse JSON "
            f"for session '{request.session_id}': {last_error}"
        )


def _initial_llm_request(
    request: PlannerRequest,
    *,
    temperature: float,
    max_tokens: int,
) -> LLMRequest:
    return LLMRequest(
        messages=(
            LLMMessage(role="system", content=_system_prompt()),
            LLMMessage(role="user", content=_user_prompt(request)),
        ),
        temperature=temperature,
        max_tokens=max_tokens,
        trace_id=request.trace_id,
    )


def _repair_llm_request(
    *,
    original: PlannerRequest,
    failed_content: str,
    error_detail: str,
    temperature: float,
    max_tokens: int,
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
                        "The previous planner response was rejected.",
                        f"Failure: {error_detail}",
                        "",
                        "Original request:",
                        _user_prompt(original),
                        "",
                        "Rejected response excerpt:",
                        snippet,
                        "",
                        "Return exactly one valid JSON object matching the schema.",
                        "No prose, no markdown fence, no explanation.",
                    ]
                ),
            ),
        ),
        temperature=temperature,
        max_tokens=max_tokens,
        trace_id=original.trace_id,
    )


def _system_prompt() -> str:
    return (
        "You are Model Forge's planning agent. Convert the user's request into "
        "a small JSON object that matches this exact schema: "
        "{\"subtasks\":[{\"id\":\"mech-1\",\"kind\":\"mechanical\","
        "\"description\":\"...\",\"depends_on\":[]}],"
        "\"clarifying_questions\":[]}. "
        "Allowed subtask kinds are only \"mechanical\" and \"organic\". "
        "Use a mechanical subtask for CAD-printable solid parts, cups, brackets, "
        "cases, knobs, fixtures, and emboss/deboss details. Flat decorative "
        "printables such as keyrings, keychains, charms, badges, tags, mascot "
        "silhouettes, and simple raised character details are mechanical "
        "OpenSCAD work; do not split them into an organic subtask unless the "
        "user explicitly asks for a sculpted freeform mesh. Use organic only "
        "for sculptural freeform mesh details that are not directly printable "
        "from parametric CAD. If the prompt is too ambiguous to plan, return "
        "{\"subtasks\":[],\"clarifying_questions\":[\"...\"]}. "
        "For mechanical subtasks, preserve dimensions, bounding boxes, labels, "
        "text/emboss/deboss requests, printer or slicer intent, material clues, "
        "and G-code/output intent in the subtask description. Do not add CAD "
        "syntax instructions such as module main(); the CAD coder owns DSL "
        "execution contracts. "
        "Never mix subtasks and clarifying_questions. Keep ids stable, lowercase, "
        "and unique. Respond with ONLY one JSON object, no prose."
    )


def _user_prompt(request: PlannerRequest) -> str:
    context = json.dumps(request.context, ensure_ascii=False, sort_keys=True)
    return (
        f"Session id: {request.session_id}\n"
        f"Trace id: {request.trace_id or ''}\n"
        f"Context JSON: {context}\n\n"
        f"User request:\n{request.prompt.strip()}"
    )


def _extract_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    match = _JSON_FENCE_RE.search(text)
    if match:
        text = match.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("response did not contain a JSON object")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise TypeError("planner response root must be a JSON object")
    return parsed
