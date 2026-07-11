"""Provider-switch seam for explicit CAD coder fallback attempts."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Protocol, cast

from modules.agents.cad_coder.base import BaseCADCoderAgent
from modules.agents.cad_coder.factory import (
    CADCoderAgentSettings,
    create_cad_coder_agent,
)
from modules.llm.factory import LLMProviderSettings, ProviderName, create_llm_provider

_RUNTIME_PROVIDER_NAMES = frozenset(("mock", "anthropic", "openai", "gemini"))


class CADCoderProviderSwitchError(RuntimeError):
    """Raised when a provider fallback target cannot be created explicitly."""


@dataclass(frozen=True)
class CADCoderProviderRequest:
    """Explicit target provider context for a fallback CAD generation attempt."""

    source_provider: str
    target_provider: str
    target_model: str
    trace_id: str | None = None


class CADCoderProviderFactory(Protocol):
    """Factory seam for creating a CAD coder bound to a target LLM provider."""

    def create_for_provider(
        self, request: CADCoderProviderRequest
    ) -> BaseCADCoderAgent:
        """Return a CAD coder for the requested provider/model."""


class RuntimeCADCoderProviderFactory:
    """Runtime implementation backed by existing LLM and CAD coder factories."""

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger(__name__)

    def create_for_provider(
        self, request: CADCoderProviderRequest
    ) -> BaseCADCoderAgent:
        target_provider = _provider_name_for_target_provider(request.target_provider)
        if target_provider == "mock":
            return create_cad_coder_agent(
                CADCoderAgentSettings(
                    adapter="mock",
                    adapter_label=f"fallback:{target_provider}",
                ),
                logger=self._logger,
            )

        llm_settings = _llm_settings_for_target_provider(
            target_provider,
            request.target_model,
        )
        llm_provider = create_llm_provider(llm_settings, logger=self._logger)
        return create_cad_coder_agent(
            CADCoderAgentSettings(
                adapter="prompt_based",
                adapter_label=f"fallback:{target_provider}",
            ),
            logger=self._logger,
            llm_provider=llm_provider,
        )


def _provider_name_for_target_provider(target_provider: str) -> ProviderName:
    normalized = target_provider.strip().lower()
    if normalized not in _RUNTIME_PROVIDER_NAMES:
        allowed = ", ".join(sorted(_RUNTIME_PROVIDER_NAMES))
        raise CADCoderProviderSwitchError(
            f"Unsupported CAD Coder fallback provider '{target_provider}'. "
            f"Allowed runtime providers: {allowed}."
        )
    return cast(ProviderName, normalized)


def _llm_settings_for_target_provider(
    target_provider: str,
    target_model: str,
) -> LLMProviderSettings:
    env_settings = LLMProviderSettings.from_env()
    provider = _provider_name_for_target_provider(target_provider)
    return LLMProviderSettings(
        provider=provider,
        model=target_model,
        mock_response=env_settings.mock_response,
        anthropic_api_key=env_settings.anthropic_api_key,
        openai_api_key=env_settings.openai_api_key,
        openai_base_url=env_settings.openai_base_url,
        gemini_api_key=os.environ.get("GEMINI_API_KEY")
        or env_settings.gemini_api_key,
        gemini_model=(
            target_model
            if target_provider == "gemini"
            else env_settings.gemini_model
        ),
    )


__all__ = [
    "CADCoderProviderSwitchError",
    "CADCoderProviderFactory",
    "CADCoderProviderRequest",
    "RuntimeCADCoderProviderFactory",
]
