"""Google Gemini provider adapter.

Implements ``BaseLLMProvider`` using the ``google-genai`` SDK (>= 1.0).

Fail-fast semantics:
- The SDK is imported lazily inside ``__init__`` so the rest of the
  codebase does not depend on the SDK being installed when the mock
  provider is selected. Missing SDK raises ``LLMProviderConfigError``.
- ``api_key`` is required at construction time. The factory pulls it
  from ``GEMINI_API_KEY``. Missing key → ``LLMProviderConfigError``.
- ``model`` is required (pin via ``GEMINI_MODEL`` or ``LLM_MODEL``).
  The adapter never silently picks a "latest" model — DESIGN.md R10
  forbids silent drift.
- Network/timeout/rate-limit errors are mapped onto the existing
  ``LLMProviderError`` hierarchy so callers can keep a single
  exception contract.
- ``provider_options`` cannot override request-critical keys
  (``system_instruction``, ``temperature``, ``max_output_tokens``)
  to keep pinning and role normalization enforceable.

Role mapping (Gemini API <-> Model Forge LLMMessage):
  - ``system``    -> extracted into config["system_instruction"]
  - ``user``      -> {"role": "user", "parts": [{"text": ...}]}
  - ``assistant`` -> {"role": "model", "parts": [{"text": ...}]}  (Gemini calls it "model")
  - ``tool``      -> ``LLMProviderConfigError`` (not supported in this phase)

Request/config representation uses plain Python dicts throughout so that
``google-genai`` SDK types are only required at construction time (for
``genai.Client``), not at every call site. The SDK accepts both typed objects
and dicts (``ContentUnionDict`` / ``GenerateContentConfigOrDict`` overloads).
"""

from __future__ import annotations

from typing import Any

from modules.llm.base import BaseLLMProvider
from modules.llm.exceptions import (
    LLMProviderConfigError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from modules.llm.schemas import LLMRequest, LLMResponse, LLMUsage

__all__ = ["GeminiLLMProvider"]

_RESERVED_CONFIG_KEYS = frozenset(
    {"system_instruction", "temperature", "max_output_tokens"}
)

# Gemini API role names differ from OpenAI/Anthropic conventions.
_ROLE_MAP = {"user": "user", "assistant": "model"}


class GeminiLLMProvider(BaseLLMProvider):
    """Google Gemini ``generate_content`` adapter (google-genai SDK >= 1.0).

    Uses plain-dict representations for ``contents`` and ``config`` so that
    SDK type imports are confined to construction time only. The SDK accepts
    both typed objects and dicts (``ContentUnionDict`` /
    ``GenerateContentConfigOrDict``).
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        client: Any | None = None,
        default_max_output_tokens: int = 8192,
    ) -> None:
        if not api_key:
            raise LLMProviderConfigError(
                "GeminiLLMProvider requires a non-empty api_key. "
                "Set GEMINI_API_KEY."
            )
        if not model:
            raise LLMProviderConfigError(
                "GeminiLLMProvider requires an explicit model. "
                "Set GEMINI_MODEL (or LLM_MODEL) to a pinned Gemini model "
                "identifier (e.g. gemini-2.5-pro-preview-05-06)."
            )

        if client is None:
            try:
                from google import genai
            except ImportError as exc:  # pragma: no cover - exercised via factory
                raise LLMProviderConfigError(
                    "google-genai SDK is not installed. Run "
                    "`pip install google-genai` or add it to the "
                    "deployment requirements before selecting "
                    "LLM_PROVIDER=gemini."
                ) from exc
            client = genai.Client(api_key=api_key)

        self._client = client
        self._model = model
        self._default_max_output_tokens = default_max_output_tokens

    @property
    def provider_name(self) -> str:
        return "gemini"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        system_instruction, contents = _build_contents(request)
        model = request.model or self._model
        max_tokens = request.max_tokens or self._default_max_output_tokens

        config: dict[str, Any] = {
            "temperature": request.temperature,
            "max_output_tokens": max_tokens,
        }
        if system_instruction is not None:
            config["system_instruction"] = system_instruction
        _merge_provider_options(config, request.provider_options)

        try:
            response = await self._client.aio.models.generate_content(
                model=model,
                contents=contents,  # type: ignore[arg-type]
                config=config,  # type: ignore[arg-type]
            )
        except TimeoutError as exc:
            raise LLMTimeoutError(
                f"Gemini request timed out for model '{model}'."
            ) from exc
        except Exception as exc:
            mapped = _map_gemini_exception(exc)
            if mapped is not None:
                raise mapped from exc
            raise LLMProviderError(
                f"Gemini request failed: {exc!s}"
            ) from exc

        try:
            content = _extract_text(response)
            if not content:
                raise LLMProviderError(
                    "Gemini returned an empty response; refusing to fabricate content."
                )
            usage = _extract_usage(response)
            finish_reason = _extract_finish_reason(response)
        except LLMProviderError:
            raise
        except Exception as exc:
            raise LLMProviderError(
                f"Gemini response normalization failed: {exc!s}"
            ) from exc

        return LLMResponse(
            provider=self.provider_name,
            model=model,
            content=content,
            usage=usage,
            trace_id=request.trace_id,
            finish_reason=finish_reason,
            raw_metadata={},
        )


def _build_contents(
    request: LLMRequest,
) -> tuple[str | None, list[dict[str, Any]]]:
    """Split messages into (system_instruction, gemini_contents_dicts).

    Gemini API does not accept a 'system' role in Contents. System messages
    are extracted into a single system_instruction string. User/assistant
    messages are mapped to user/model roles. 'tool' raises
    LLMProviderConfigError (not supported in this phase).

    Returns plain Python dicts compatible with the SDK's ContentUnionDict
    overload so that google-genai types are not imported at call time.
    """
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []

    for message in request.messages:
        if message.role == "system":
            system_parts.append(message.content)
            continue
        if message.role == "tool":
            raise LLMProviderConfigError(
                "GeminiLLMProvider does not yet support 'tool' role "
                "messages. Translate tool results upstream before calling."
            )
        gemini_role = _ROLE_MAP.get(message.role)
        if gemini_role is None:
            raise LLMProviderConfigError(
                f"GeminiLLMProvider does not support role '{message.role}'."
            )
        contents.append({"role": gemini_role, "parts": [{"text": message.content}]})

    if not contents:
        raise LLMProviderConfigError(
            "GeminiLLMProvider requires at least one user/assistant "
            "message; got only system messages."
        )

    system_instruction = "\n\n".join(system_parts) if system_parts else None
    return system_instruction, contents


def _extract_text(response: Any) -> str:
    # google-genai SDK: response.text is a convenience property that
    # concatenates all text parts from the first candidate.
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    # Fallback: walk candidates -> content -> parts manually.
    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or []
        pieces: list[str] = []
        for part in parts:
            t = getattr(part, "text", None)
            if isinstance(t, str) and t:
                pieces.append(t)
        if pieces:
            return "".join(pieces)
    return ""


def _extract_usage(response: Any) -> LLMUsage:
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return LLMUsage(input_tokens=0, output_tokens=0)
    candidate_tokens = int(getattr(meta, "candidates_token_count", 0) or 0)
    # Gemini bills output with thinking tokens included. The SDK exposes
    # thoughts separately, so fold them into the normalized output total.
    thought_tokens = int(getattr(meta, "thoughts_token_count", 0) or 0)
    return LLMUsage(
        input_tokens=int(getattr(meta, "prompt_token_count", 0) or 0),
        output_tokens=candidate_tokens + thought_tokens,
    )


def _extract_finish_reason(response: Any) -> str | None:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return None
    reason = getattr(candidates[0], "finish_reason", None)
    if reason is None:
        return None
    # FinishReason enum -> string
    return str(reason.name) if hasattr(reason, "name") else str(reason)


def _merge_provider_options(
    config: dict[str, Any], provider_options: dict[str, Any]
) -> None:
    reserved = _RESERVED_CONFIG_KEYS.intersection(provider_options)
    if reserved:
        names = ", ".join(sorted(reserved))
        raise LLMProviderConfigError(
            "Gemini provider_options cannot override core request "
            f"fields: {names}. Use LLMRequest fields instead so model "
            "pinning and role normalization remain enforced."
        )
    config.update(provider_options)


def _map_gemini_exception(exc: Exception) -> LLMProviderError | None:
    name = type(exc).__name__
    # google-api-core exception names
    if name in {"ResourceExhausted", "TooManyRequests", "RateLimitError"}:
        return LLMRateLimitError(str(exc))
    if name in {"DeadlineExceeded", "APITimeoutError", "TimeoutError"}:
        return LLMTimeoutError(str(exc))
    if name in {
        "Unauthenticated",
        "PermissionDenied",
        "AuthenticationError",
        "PermissionDeniedError",
        "InvalidAPIKeyError",
    }:
        return LLMProviderConfigError(str(exc))
    return None
