"""Ollama chat provider adapter for local LLM smoke tests.

The adapter is intentionally a thin ``BaseLLMProvider`` implementation over
Ollama's local ``/api/chat`` endpoint. It does not require API keys, but it
still requires an explicit model pin such as ``qwen2.5-coder:7b`` so local
model drift is visible in evidence.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any, Protocol

from modules.llm.base import BaseLLMProvider
from modules.llm.exceptions import (
    LLMProviderConfigError,
    LLMProviderError,
    LLMTimeoutError,
)
from modules.llm.schemas import LLMRequest, LLMResponse, LLMUsage

__all__ = ["OllamaLLMProvider"]

_RESERVED_PROVIDER_OPTION_KEYS = frozenset({"model", "messages", "stream"})
_RESERVED_OPTIONS_KEYS = frozenset({"temperature", "num_predict"})
_ALLOWED_ROLES = frozenset({"system", "user", "assistant"})


class _OllamaChatClient(Protocol):
    def chat(self, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        """Submit a synchronous Ollama chat request."""


class _UrlLibOllamaChatClient:
    def __init__(self, base_url: str) -> None:
        self._endpoint = _normalize_base_url(base_url) + "/api/chat"

    def chat(self, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self._endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                raw = response.read().decode("utf-8")
        except TimeoutError as exc:
            raise LLMTimeoutError("Ollama request timed out.") from exc
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code == 404:
                raise LLMProviderConfigError(
                    "Ollama model or endpoint was not found. "
                    f"Response: {detail or exc.reason}"
                ) from exc
            raise LLMProviderError(
                f"Ollama HTTP {exc.code}: {detail or exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise LLMProviderConfigError(
                "Could not reach Ollama. Start Ollama and verify OLLAMA_BASE_URL."
            ) from exc

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMProviderError("Ollama returned non-JSON response.") from exc
        if not isinstance(parsed, dict):
            raise LLMProviderError("Ollama returned unexpected response shape.")
        return parsed


class OllamaLLMProvider(BaseLLMProvider):
    """Local Ollama ``/api/chat`` adapter."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str = "http://127.0.0.1:11434",
        timeout_s: float = 120.0,
        client: _OllamaChatClient | None = None,
    ) -> None:
        if not model:
            raise LLMProviderConfigError(
                "OllamaLLMProvider requires an explicit model. "
                "Set LLM_MODEL to an installed Ollama model such as "
                "qwen2.5-coder:7b."
            )
        if timeout_s <= 0:
            raise LLMProviderConfigError("OLLAMA_TIMEOUT_S must be positive.")
        self._model = model
        self._timeout_s = timeout_s
        self._client = client or _UrlLibOllamaChatClient(base_url)

    @property
    def provider_name(self) -> str:
        return "ollama"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        model = request.model or self._model
        payload = _build_payload(request, model=model)

        try:
            response = await asyncio.to_thread(
                self._client.chat,
                payload,
                timeout_s=self._timeout_s,
            )
        except LLMProviderError:
            raise
        except Exception as exc:
            raise LLMProviderError(f"Ollama request failed: {exc!s}") from exc

        content = _extract_text(response)
        if not content:
            raise LLMProviderError(
                "Ollama returned an empty response; refusing to fabricate content."
            )

        return LLMResponse(
            provider=self.provider_name,
            model=str(response.get("model") or model),
            content=content,
            usage=_extract_usage(response),
            trace_id=request.trace_id,
            finish_reason=_extract_finish_reason(response),
            raw_metadata={
                "total_duration_ns": response.get("total_duration"),
                "load_duration_ns": response.get("load_duration"),
            },
        )


def _normalize_base_url(base_url: str) -> str:
    stripped = base_url.strip().rstrip("/")
    if not stripped:
        raise LLMProviderConfigError("OLLAMA_BASE_URL must not be empty.")
    return stripped


def _build_payload(request: LLMRequest, *, model: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": _normalize_messages(request),
        "stream": False,
        "options": {
            "temperature": request.temperature,
            "num_predict": request.max_tokens,
        },
    }
    if request.max_tokens is None:
        payload["options"].pop("num_predict")
    _merge_provider_options(payload, request.provider_options)
    return payload


def _normalize_messages(request: LLMRequest) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for message in request.messages:
        if message.role not in _ALLOWED_ROLES:
            raise LLMProviderConfigError(
                f"OllamaLLMProvider does not support role '{message.role}'."
            )
        messages.append({"role": message.role, "content": message.content})
    if not messages:
        raise LLMProviderConfigError("OllamaLLMProvider requires at least one message.")
    return messages


def _merge_provider_options(
    payload: dict[str, Any],
    provider_options: dict[str, Any],
) -> None:
    reserved = _RESERVED_PROVIDER_OPTION_KEYS.intersection(provider_options)
    if reserved:
        names = ", ".join(sorted(reserved))
        raise LLMProviderConfigError(
            "Ollama provider_options cannot override core request fields: "
            f"{names}. Use LLMRequest fields instead."
        )
    extra_options = provider_options.get("options")
    if extra_options is not None:
        if not isinstance(extra_options, dict):
            raise LLMProviderConfigError(
                "Ollama provider_options['options'] must be a mapping."
            )
        reserved_options = _RESERVED_OPTIONS_KEYS.intersection(extra_options)
        if reserved_options:
            names = ", ".join(sorted(reserved_options))
            raise LLMProviderConfigError(
                "Ollama provider_options['options'] cannot override request "
                f"fields: {names}."
            )
        payload["options"].update(extra_options)
    for key, value in provider_options.items():
        if key == "options":
            continue
        payload[key] = value


def _extract_text(response: dict[str, Any]) -> str:
    message = response.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _extract_usage(response: dict[str, Any]) -> LLMUsage:
    return LLMUsage(
        input_tokens=int(response.get("prompt_eval_count") or 0),
        output_tokens=int(response.get("eval_count") or 0),
    )


def _extract_finish_reason(response: dict[str, Any]) -> str | None:
    reason = response.get("done_reason")
    return reason if isinstance(reason, str) else None
