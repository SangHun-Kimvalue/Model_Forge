from abc import ABC, abstractmethod

from modules.llm.schemas import LLMRequest, LLMResponse

__all__ = ["BaseLLMProvider"]


class BaseLLMProvider(ABC):
    """Contract for LLM provider adapters.

    Preconditions:
        request.messages contains at least one message and request.provider_options
        is already validated at the application boundary.
    Postconditions:
        Returns a provider-normalized response with usage populated, even when
        the upstream provider does not expose exact token counts.
    Raises:
        LLMProviderError subclasses for provider, configuration, timeout, and
        rate-limit failures.
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Stable provider identifier used in logs, traces, and tests."""

    @abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Complete a non-streaming chat request."""
