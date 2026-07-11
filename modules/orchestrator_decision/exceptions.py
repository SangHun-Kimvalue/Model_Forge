from __future__ import annotations

from modules.orchestrator_decision.schemas import DecisionResult


class OrchestratorDecisionError(Exception):
    """Raised when a decision result cannot be executed in the current phase."""

    stage = "decision"

    def __init__(
        self,
        message: str,
        *,
        decision_result: DecisionResult,
    ) -> None:
        super().__init__(message)
        self.decision_action = decision_result.action.value
        self.decision_reason = decision_result.reason.value
        self.decision_detail = decision_result.detail
        self.decision_metadata = decision_result.model_dump(mode="json")


__all__ = ["OrchestratorDecisionError"]
