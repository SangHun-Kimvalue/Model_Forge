from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveInt

LLMRole = Literal["system", "user", "assistant", "tool"]

__all__ = [
    "LLMMessage",
    "LLMRequest",
    "LLMResponse",
    "LLMRole",
    "LLMUsage",
]


class LLMMessage(BaseModel):
    """Provider-neutral chat message."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: LLMRole
    content: str = Field(min_length=1)
    name: str | None = None


class LLMRequest(BaseModel):
    """Provider-neutral completion request.

    Preconditions:
        messages must be non-empty and ordered exactly as the agent wants the
        provider to see them.
    Raises:
        pydantic.ValidationError if required fields are absent or malformed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    messages: tuple[LLMMessage, ...] = Field(min_length=1)
    model: str | None = None
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: PositiveInt | None = None
    trace_id: str | None = None
    provider_options: dict[str, Any] = Field(default_factory=dict)


class LLMUsage(BaseModel):
    """Normalized token usage for cost and trace reporting."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class LLMResponse(BaseModel):
    """Provider-neutral completion response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    model: str
    content: str = Field(min_length=1)
    usage: LLMUsage
    trace_id: str | None = None
    finish_reason: str | None = None
    raw_metadata: dict[str, Any] = Field(default_factory=dict)
