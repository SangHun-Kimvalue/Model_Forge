"""OpenAI Chat Completions provider adapter (Phase 8B).

Implements ``BaseLLMProvider`` using the official ``openai`` Python SDK
(``openai.AsyncOpenAI``).

Fail-fast semantics:
- The SDK is imported lazily inside ``__init__`` so the rest of the
  codebase does not depend on the SDK being installed when the mock
  provider is selected. Missing SDK raises ``LLMProviderConfigError``.
- ``api_key`` is required at construction time. The factory pulls it
  from ``OPENAI_API_KEY``. Missing key → ``LLMProviderConfigError``.
- ``model`` is required (pin via ``LLM_MODEL``). The adapter never
  silently picks a "latest" model — DESIGN.md R10 forbids silent
  drift.
- Network/timeout/rate-limit errors are mapped onto the existing
  ``LLMProviderError`` hierarchy so callers can keep a single
  exception contract.
- ``provider_options`` cannot override request-critical keys
  (``model``, ``messages``, ``max_tokens``, ``temperature``) to keep
  pinning and role normalization enforceable.
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

__all__ = ["OpenAILLMProvider"]

_RESERVED_PROVIDER_OPTION_KEYS = frozenset(
    {"model", "messages", "max_tokens", "temperature"}
)
_ALLOWED_ROLES = frozenset({"system", "user", "assistant", "tool"})


class OpenAILLMProvider(BaseLLMProvider):
    """OpenAI ``chat.completions.create()`` adapter."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        client: Any | None = None,
        default_max_tokens: int = 1024,
        base_url: str | None = None,
    ) -> None:
        if not api_key:
            raise LLMProviderConfigError(
                "OpenAILLMProvider requires a non-empty api_key. "
                "Set OPENAI_API_KEY."
            )
        if not model:
            raise LLMProviderConfigError(
                "OpenAILLMProvider requires an explicit model. "
                "Set LLM_MODEL to a pinned OpenAI model identifier."
            )

        if client is None:
            try:
                import openai  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover - exercised via factory
                raise LLMProviderConfigError(
                    "openai SDK is not installed. Run "
                    "`pip install openai` or pin the SDK in your "
                    "deployment requirements before selecting "
                    "LLM_PROVIDER=openai."
                ) from exc
            client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)

        self._client = client
        self._model = model
        self._default_max_tokens = default_max_tokens

    @property
    def provider_name(self) -> str:
        return "openai"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        messages = _normalize_messages(request)
        model = request.model or self._model
        max_tokens = request.max_tokens or self._default_max_tokens

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": request.temperature,
        }
        _merge_provider_options(kwargs, request.provider_options)

        try:
            response = await self._client.chat.completions.create(**kwargs)
        except TimeoutError as exc:
            raise LLMTimeoutError(
                f"OpenAI request timed out for model '{model}'."
            ) from exc
        except Exception as exc:
            mapped = _map_openai_exception(exc)
            if mapped is not None:
                raise mapped from exc
            raise LLMProviderError(
                f"OpenAI request failed: {exc!s}"
            ) from exc

        content = _extract_text(response)
        if not content:
            raise LLMProviderError(
                "OpenAI returned an empty response; refusing to fabricate content."
            )

        usage = _extract_usage(response)
        return LLMResponse(
            provider=self.provider_name,
            model=getattr(response, "model", model),
            content=content,
            usage=usage,
            trace_id=request.trace_id,
            finish_reason=_extract_finish_reason(response),
            raw_metadata={"id": getattr(response, "id", None)},
        )


def _normalize_messages(request: LLMRequest) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for message in request.messages:
        if message.role not in _ALLOWED_ROLES:
            raise LLMProviderConfigError(
                f"OpenAILLMProvider does not support role '{message.role}'."
            )
        entry: dict[str, str] = {"role": message.role, "content": message.content}
        if message.name is not None:
            entry["name"] = message.name
        messages.append(entry)
    if not messages:
        raise LLMProviderConfigError(
            "OpenAILLMProvider requires at least one message."
        )
    return messages


def _extract_text(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    first = choices[0]
    message = getattr(first, "message", None)
    if message is None:
        return ""
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            text = getattr(block, "text", None) or (
                block.get("text") if isinstance(block, dict) else None
            )
            if isinstance(text, str) and text:
                parts.append(text)
        return "".join(parts)
    return ""


def _extract_finish_reason(response: Any) -> str | None:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return None
    return getattr(choices[0], "finish_reason", None)


def _extract_usage(response: Any) -> LLMUsage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return LLMUsage(input_tokens=0, output_tokens=0)
    # OpenAI: prompt_tokens / completion_tokens
    return LLMUsage(
        input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
    )


def _merge_provider_options(
    kwargs: dict[str, Any], provider_options: dict[str, Any]
) -> None:
    reserved = _RESERVED_PROVIDER_OPTION_KEYS.intersection(provider_options)
    if reserved:
        names = ", ".join(sorted(reserved))
        raise LLMProviderConfigError(
            "OpenAI provider_options cannot override core request "
            f"fields: {names}. Use LLMRequest fields instead so model "
            "pinning and message-role normalization remain enforced."
        )
    kwargs.update(provider_options)


def _map_openai_exception(exc: Exception) -> LLMProviderError | None:
    name = type(exc).__name__
    if name in {"RateLimitError", "APIRateLimitError"}:
        return LLMRateLimitError(str(exc))
    if name in {"APITimeoutError", "TimeoutError"}:
        return LLMTimeoutError(str(exc))
    if name in {"AuthenticationError", "PermissionDeniedError"}:
        return LLMProviderConfigError(str(exc))
    return None
