from modules.agents.self_healer.base import BaseSelfHealerAgent
from modules.agents.self_healer.schemas import (
    HealableErrorKind,
    SelfHealerRequest,
    SelfHealerResponse,
)


class MockSelfHealerAgent(BaseSelfHealerAgent):
    """Deterministic self-healer for unit tests and local scaffolding.

    Policy:
      - If attempt < max_attempts → retry_with_revision with an error-kind
        specific instruction.
      - If attempt >= max_attempts → escalate_to_human (P8 — bounded retries).
    """

    default_adapter_name = "mock-self-healer-v1"

    def __init__(self, adapter_label: str | None = None) -> None:
        self._adapter_label = adapter_label or self.default_adapter_name

    @property
    def adapter_name(self) -> str:
        return self._adapter_label

    async def heal(self, request: SelfHealerRequest) -> SelfHealerResponse:
        if request.attempt >= request.max_attempts:
            return SelfHealerResponse(
                action="escalate_to_human",
                reason=(
                    f"max_attempts={request.max_attempts} reached for "
                    f"subtask {request.failed_subtask_id} "
                    f"({request.error_kind})"
                ),
                trace_id=request.trace_id,
            )

        revision = _revision_hint(request.error_kind, request.original_request_summary)
        return SelfHealerResponse(
            action="retry_with_revision",
            reason=(
                f"attempt {request.attempt}/{request.max_attempts} of "
                f"subtask {request.failed_subtask_id} failed with "
                f"{request.error_kind}; revising and retrying"
            ),
            revised_instruction=revision,
            trace_id=request.trace_id,
        )


def _revision_hint(kind: HealableErrorKind, summary: str) -> str:
    base = f"Original request: {summary}"
    if kind == "cad_code_error":
        return f"{base}\nRevision: fix syntax/imports and re-emit the script."
    if kind == "semantic_mismatch":
        return (
            f"{base}\nRevision: preserve the same user intent, but explicitly "
            "model every missing required feature as real geometry before "
            "returning the complete script."
        )
    if kind == "topology_error":
        return (
            f"{base}\nRevision: avoid degenerate geometry; ensure manifold "
            "boolean operations."
        )
    if kind == "timeout":
        return f"{base}\nRevision: reduce mesh resolution / iteration count."
    return f"{base}\nRevision: retry with the same parameters."
