from abc import ABC, abstractmethod

from modules.agents.self_healer.schemas import SelfHealerRequest, SelfHealerResponse


class BaseSelfHealerAgent(ABC):
    """Contract for failed-attempt remediation.

    Preconditions:
        request.attempt <= request.max_attempts; callers own the retry counter.
    Postconditions:
        - action == 'retry_with_revision' implies revised_instruction is set.
        - action == 'escalate_to_human' implies revised_instruction is None.
    Raises:
        SelfHealerAgentError subclasses on configuration or remediation failure.
    """

    @property
    @abstractmethod
    def adapter_name(self) -> str:
        """Stable adapter identifier used in logs, traces, and tests."""

    @abstractmethod
    async def heal(self, request: SelfHealerRequest) -> SelfHealerResponse:
        """Decide whether to revise and retry, or to escalate to a human."""
