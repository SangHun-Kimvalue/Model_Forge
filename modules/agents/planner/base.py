from abc import ABC, abstractmethod

from modules.agents.planner.schemas import PlannerRequest, PlannerResponse


class BasePlannerAgent(ABC):
    """Contract for natural-language → subtask planners.

    Preconditions:
        request.prompt is already sanitized at the application boundary.
    Postconditions:
        Either subtasks is non-empty or clarifying_questions is non-empty
        (P4 — never silently invent subtasks for ambiguous prompts).
    Raises:
        PlannerAgentError subclasses on configuration or planning failure.
    """

    @property
    @abstractmethod
    def adapter_name(self) -> str:
        """Stable adapter identifier used in logs, traces, and tests."""

    @abstractmethod
    async def plan(self, request: PlannerRequest) -> PlannerResponse:
        """Decompose a user prompt into ordered subtasks."""
