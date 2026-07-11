from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

HealableErrorKind = Literal[
    "cad_code_error",
    "semantic_mismatch",
    "topology_error",
    "timeout",
    "other",
]

HealerAction = Literal["retry_with_revision", "escalate_to_human"]


class SelfHealerRequest(BaseModel):
    """Provider-neutral self-healer input.

    Preconditions:
        attempt is 1-based and never exceeds max_attempts. Callers must
        increment attempt before re-invoking the healer (the agent does not
        own retry counters).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    failed_subtask_id: str = Field(min_length=1)
    error_kind: HealableErrorKind
    error_message: str = Field(min_length=1)
    attempt: PositiveInt
    max_attempts: PositiveInt
    original_request_summary: str = Field(min_length=1)
    trace_id: str | None = None

    @model_validator(mode="after")
    def validate_attempt_contract(self) -> "SelfHealerRequest":
        if self.attempt > self.max_attempts:
            raise ValueError("attempt must not exceed max_attempts.")
        return self


class SelfHealerResponse(BaseModel):
    """Provider-neutral self-healer output.

    Postconditions:
        When action == 'retry_with_revision', revised_instruction must be
        non-empty. When action == 'escalate_to_human', revised_instruction is
        None and reason explains the escalation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: HealerAction
    reason: str = Field(min_length=1)
    revised_instruction: str | None = None
    trace_id: str | None = None

    @model_validator(mode="after")
    def validate_action_contract(self) -> "SelfHealerResponse":
        if self.action == "retry_with_revision":
            if self.revised_instruction is None or not self.revised_instruction.strip():
                raise ValueError(
                    "retry_with_revision requires a non-empty revised_instruction."
                )
        if self.action == "escalate_to_human" and self.revised_instruction is not None:
            raise ValueError("escalate_to_human must not include revised_instruction.")
        return self
