from __future__ import annotations

from abc import ABC, abstractmethod

from modules.orchestrator_decision.schemas import DecisionRequest, DecisionResult


class BaseDecisionEngine(ABC):
    """Contract for selecting the next orchestrator action from quality signals."""

    @property
    @abstractmethod
    def engine_name(self) -> str:
        """Human-readable engine identifier."""

    @abstractmethod
    def decide(self, request: DecisionRequest) -> DecisionResult:
        """Return a deterministic decision without performing the action."""


__all__ = ["BaseDecisionEngine"]
