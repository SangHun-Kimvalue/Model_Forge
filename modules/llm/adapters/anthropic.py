"""Anthropic Claude provider adapter (Phase 8B).

Implements ``BaseLLMProvider`` using the official ``anthropic`` SDK.

Fail-fast semantics:
- The SDK is imported lazily inside ``__init__`` so the rest of the
  codebase does not depend on the SDK being installed when the mock
  provider is selected. Missing SDK raises ``LLMProviderConfigError``.
- ``api_key`` is required at construction time. The factory pulls it
  from ``ANTHROPIC_API_KEY``. Missing key → ``LLMProviderConfigError``.
- ``model`` is required (pin via ``LLM_MODEL``). The adapter never
  silently picks a "latest" model — DESIGN.md R10 forbids silent
  drift.
- Network/timeout/rate-limit errors are mapped onto the existing
  ``LLMProviderError`` hierarchy so callers can keep a single
  exception contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from modules.llm.base import BaseLLMProvider
from modules.llm.exceptions import (
    LLMProviderConfigError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from modules.llm.schemas import LLMRequest, LLMResponse, LLMUsage

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

__all__ = ["AnthropicLLMProvider"]

_RESERVED_PROVIDER_OPTION_KEYS = frozenset(
    {"model", "messages", "system", "max_tokens", "temperature"}
)


class AnthropicLLMProvider(BaseLLMProvider):
    """Anthropic Claude messages.create() adapter."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        client: Any | None = None,
        default_max_tokens: int = 1024,
    ) -> None:
        if not api_key:
            raise LLMProviderConfigError(
                "AnthropicLLMProvider requires a non-empty api_key. "
                "Set ANTHROPIC_API_KEY."
            )
        if not model:
            raise LLMProviderConfigError(
                "AnthropicLLMProvider requires an explicit model. "
                "Set LLM_MODEL to a pinned Claude model identifier."
            )

        if client is None:
            try:
                import anthropic  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover - exercised via factory
                raise LLMProviderConfigError(
                    "anthropic SDK is not installed. Run "
                    "`pip install anthropic` or pin the SDK in your "
                    "deployment requirements before selecting "
                    "LLM_PROVIDER=anthropic."
                ) from exc
            client = anthropic.AsyncAnthropic(api_key=api_key)

        self._client = client
        self._model = model
        self._default_max_tokens = default_max_tokens

    @property
    def provider_name(self) -> str:
        return "anthropic"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        system_text, messages = _split_messages(request)
        model = request.model or self._model
        max_tokens = request.max_tokens or self._default_max_tokens

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": request.temperature,
        }
        if system_text is not None:
            kwargs["system"] = system_text
        _merge_provider_options(kwargs, request.provider_options)

        try:
            response = await self._client.messages.create(**kwargs)
        except TimeoutError as exc:
            raise LLMTimeoutError(
                f"Anthropic request timed out for model '{model}'."
            ) from exc
        except Exception as exc:
            mapped = _map_anthropic_exception(exc)
            if mapped is not None:
                raise mapped from exc
            raise LLMProviderError(
                f"Anthropic request failed: {exc!s}"
            ) from exc

        content = _extract_text(response)
        if not content:
            raise LLMProviderError(
                "Anthropic returned an empty response; refusing to fabricate content."
            )

        usage = _extract_usage(response)
        return LLMResponse(
            provider=self.provider_name,
            model=getattr(response, "model", model),
            content=content,
            usage=usage,
            trace_id=request.trace_id,
            finish_reason=getattr(response, "stop_reason", None),
            raw_metadata={"id": getattr(response, "id", None)},
        )


def _split_messages(
    request: LLMRequest,
) -> tuple[str | None, list[dict[str, str]]]:
    system_parts: list[str] = []
    messages: list[dict[str, str]] = []
    for message in request.messages:
        if message.role == "system":
            system_parts.append(message.content)
            continue
        if message.role == "tool":
            # Anthropic uses dedicated tool_result blocks; not supported in
            # this Phase 8B skeleton. Surface as config error rather than
            # silently dropping content.
            raise LLMProviderConfigError(
                "AnthropicLLMProvider does not yet support 'tool' role "
                "messages. Translate tool results upstream before calling."
            )
        messages.append({"role": message.role, "content": message.content})
    if not messages:
        raise LLMProviderConfigError(
            "AnthropicLLMProvider requires at least one user/assistant "
            "message; got only system messages."
        )
    system_text = "\n\n".join(system_parts) if system_parts else None
    return system_text, messages


def _extract_text(response: Any) -> str:
    blocks = getattr(response, "content", None) or []
    parts: list[str] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
    return "".join(parts)


def _extract_usage(response: Any) -> LLMUsage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return LLMUsage(input_tokens=0, output_tokens=0)
    return LLMUsage(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
    )


def _merge_provider_options(
    kwargs: dict[str, Any], provider_options: dict[str, Any]
) -> None:
    reserved = _RESERVED_PROVIDER_OPTION_KEYS.intersection(provider_options)
    if reserved:
        names = ", ".join(sorted(reserved))
        raise LLMProviderConfigError(
            "Anthropic provider_options cannot override core request "
            f"fields: {names}. Use LLMRequest fields instead so model "
            "pinning and message-role normalization remain enforced."
        )
    kwargs.update(provider_options)


def _map_anthropic_exception(exc: Exception) -> LLMProviderError | None:
    name = type(exc).__name__
    if name in {"RateLimitError", "APIRateLimitError"}:
        return LLMRateLimitError(str(exc))
    if name in {"APITimeoutError", "TimeoutError"}:
        return LLMTimeoutError(str(exc))
    if name in {"AuthenticationError", "PermissionDeniedError"}:
        return LLMProviderConfigError(str(exc))
    return None
