import logging
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from modules.llm.adapters.mock import MockLLMProvider
from modules.llm.base import BaseLLMProvider
from modules.llm.exceptions import LLMProviderConfigError
from modules.observability.events import mock_selected_event, mock_selected_extra

ProviderName = Literal["mock", "anthropic", "openai", "gemini", "ollama", "local"]

__all__ = [
    "LLMProviderSettings",
    "ProviderName",
    "create_llm_provider",
]


class LLMProviderSettings(BaseModel):
    """Runtime settings for selecting an LLM provider.

    Preconditions:
        provider must name the provider the caller actually intends to use.
        The factory never downgrades an unsupported provider to mock.
    Raises:
        LLMProviderConfigError when the selected provider has no concrete
        implementation in this build.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: ProviderName = Field(default="mock")
    model: str | None = Field(default=None)
    mock_response: str | None = Field(default=None)
    anthropic_api_key: str | None = Field(default=None)
    openai_api_key: str | None = Field(default=None)
    openai_base_url: str | None = Field(default=None)
    gemini_api_key: str | None = Field(default=None)
    gemini_model: str | None = Field(default=None)
    ollama_base_url: str = Field(default="http://127.0.0.1:11434")
    ollama_timeout_s: float = Field(default=120.0, gt=0.0)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "LLMProviderSettings":
        source = environ if environ is not None else os.environ
        return cls(
            provider=source.get("LLM_PROVIDER", "mock"),
            model=source.get("LLM_MODEL"),
            mock_response=source.get("LLM_MOCK_RESPONSE"),
            anthropic_api_key=source.get("ANTHROPIC_API_KEY"),
            openai_api_key=source.get("OPENAI_API_KEY"),
            openai_base_url=source.get("OPENAI_BASE_URL"),
            gemini_api_key=source.get("GEMINI_API_KEY"),
            gemini_model=source.get("GEMINI_MODEL"),
            ollama_base_url=source.get(
                "OLLAMA_BASE_URL", "http://127.0.0.1:11434"
            ),
            ollama_timeout_s=float(source.get("OLLAMA_TIMEOUT_S", "120")),
        )


def create_llm_provider(
    settings: LLMProviderSettings | None = None,
    logger: logging.Logger | None = None,
) -> BaseLLMProvider:
    selected = settings or LLMProviderSettings.from_env()
    log = logger or logging.getLogger(__name__)

    if selected.provider == "mock":
        label = selected.model or MockLLMProvider.default_model
        log.warning(
            mock_selected_event("llm_provider"),
            extra=mock_selected_extra(
                component="llm_provider",
                adapter="mock",
                label=label,
            ),
        )
        return MockLLMProvider(model=selected.model, canned_response=selected.mock_response)

    if selected.provider == "anthropic":
        from modules.llm.adapters.anthropic import AnthropicLLMProvider

        if not selected.anthropic_api_key:
            raise LLMProviderConfigError(
                "LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY. "
                "Mock fallback is forbidden (DESIGN.md R10)."
            )
        if not selected.model:
            raise LLMProviderConfigError(
                "LLM_PROVIDER=anthropic requires LLM_MODEL pinned to a "
                "specific Claude model identifier (e.g. "
                "claude-sonnet-4-5-20250929)."
            )
        return AnthropicLLMProvider(
            api_key=selected.anthropic_api_key,
            model=selected.model,
        )

    if selected.provider == "openai":
        from modules.llm.adapters.openai import OpenAILLMProvider

        if not selected.openai_api_key:
            raise LLMProviderConfigError(
                "LLM_PROVIDER=openai requires OPENAI_API_KEY. "
                "Mock fallback is forbidden (DESIGN.md R10)."
            )
        if not selected.model:
            raise LLMProviderConfigError(
                "LLM_PROVIDER=openai requires LLM_MODEL pinned to a "
                "specific OpenAI model identifier (e.g. "
                "gpt-4.1-2025-04-14)."
            )
        return OpenAILLMProvider(
            api_key=selected.openai_api_key,
            model=selected.model,
            base_url=selected.openai_base_url,
        )

    if selected.provider == "gemini":
        from modules.llm.adapters.gemini import GeminiLLMProvider

        if not selected.gemini_api_key:
            raise LLMProviderConfigError(
                "LLM_PROVIDER=gemini requires GEMINI_API_KEY. "
                "Mock fallback is forbidden (DESIGN.md R10)."
            )
        # GEMINI_MODEL takes priority; fall back to LLM_MODEL if set.
        model = selected.gemini_model or selected.model
        if not model:
            raise LLMProviderConfigError(
                "LLM_PROVIDER=gemini requires GEMINI_MODEL (or LLM_MODEL) "
                "pinned to a specific Gemini model identifier (e.g. "
                "gemini-2.5-pro-preview-05-06)."
            )
        return GeminiLLMProvider(
            api_key=selected.gemini_api_key,
            model=model,
        )

    if selected.provider in {"ollama", "local"}:
        from modules.llm.adapters.ollama import OllamaLLMProvider

        if not selected.model:
            raise LLMProviderConfigError(
                "LLM_PROVIDER=ollama/local requires LLM_MODEL pinned to an "
                "installed Ollama model identifier (e.g. qwen2.5-coder:7b)."
            )
        return OllamaLLMProvider(
            model=selected.model,
            base_url=selected.ollama_base_url,
            timeout_s=selected.ollama_timeout_s,
        )

    raise LLMProviderConfigError(
        f"LLM provider '{selected.provider}' is selected but no concrete adapter exists yet. "
        "Set LLM_PROVIDER=mock for deterministic tests, or implement the provider adapter first."
    )
