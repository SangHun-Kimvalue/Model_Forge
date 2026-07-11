__all__ = [
    "LLMProviderConfigError",
    "LLMProviderError",
    "LLMRateLimitError",
    "LLMTimeoutError",
]


class LLMProviderError(RuntimeError):
    """Base class for LLM provider failures."""


class LLMProviderConfigError(LLMProviderError):
    """Provider configuration is missing, unsupported, or contradictory."""


class LLMRateLimitError(LLMProviderError):
    """The upstream provider rejected a request due to quota or rate limits."""


class LLMTimeoutError(LLMProviderError):
    """The upstream provider did not respond within the configured timeout."""
