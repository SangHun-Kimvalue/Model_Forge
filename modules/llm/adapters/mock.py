import hashlib

from modules.llm.base import BaseLLMProvider
from modules.llm.schemas import LLMRequest, LLMResponse, LLMUsage

__all__ = ["MockLLMProvider"]


class MockLLMProvider(BaseLLMProvider):
    """Deterministic provider for unit tests and local scaffolding."""

    default_model = "mock-deterministic-v1"

    def __init__(self, model: str | None = None, canned_response: str | None = None) -> None:
        self._model = model or self.default_model
        self._canned_response = canned_response

    @property
    def provider_name(self) -> str:
        return "mock"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        input_text = "\n".join(f"{message.role}: {message.content}" for message in request.messages)
        content = self._canned_response or self._deterministic_content(input_text)
        return LLMResponse(
            provider=self.provider_name,
            model=request.model or self._model,
            content=content,
            usage=LLMUsage(
                input_tokens=_rough_token_count(input_text),
                output_tokens=_rough_token_count(content),
            ),
            trace_id=request.trace_id,
            finish_reason="stop",
            raw_metadata={"deterministic": True},
        )

    @staticmethod
    def _deterministic_content(input_text: str) -> str:
        digest = hashlib.sha256(input_text.encode("utf-8")).hexdigest()[:12]
        return f"mock-response:{digest}"


def _rough_token_count(text: str) -> int:
    stripped = text.strip()
    if not stripped:
        return 0
    return len(stripped.split())
