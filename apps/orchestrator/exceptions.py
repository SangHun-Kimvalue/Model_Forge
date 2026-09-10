"""Orchestrator error taxonomy (DESIGN.md §P8).

Mirrors the per-module pattern: a single root error so callers can
``except OrchestratorError`` and typed subclasses so the orchestrator
itself can branch on cause (P8). HTTP routes translate these into
``4xx`` responses; the graph engine treats them as terminal — they do
not trigger self-healing retries.
"""

from apps.orchestrator.schemas import ClarificationQuestion
from modules.orchestrator_decision.schemas import DecisionResult
from modules.template.ip_abuse_prefilter import FallbackGateDecision


class OrchestratorError(Exception):
    """Root for every orchestrator-internal failure."""


class SessionNotFoundError(OrchestratorError):
    """Raised when a referenced session id does not exist in the store."""


class ApprovalGateNotFoundError(OrchestratorError):
    """Raised when an /approve call targets an unknown or already-resolved gate."""


class ClarificationNotFoundError(OrchestratorError):
    """Raised when a clarification answer targets no pending clarification."""


class ClarificationRequiredError(OrchestratorError):
    """Raised by a pipeline when execution must pause for user input."""

    stage = "clarification"

    def __init__(
        self,
        message: str,
        *,
        decision_result: DecisionResult,
        questions: tuple[ClarificationQuestion, ...],
    ) -> None:
        super().__init__(message)
        self.decision_action = decision_result.action.value
        self.decision_reason = decision_result.reason.value
        self.decision_detail = decision_result.detail
        self.decision_metadata = decision_result.model_dump(mode="json")
        self.questions = questions


class PrefilterBlockedError(OrchestratorError):
    """Raised by a pipeline when the IP/abuse pre-filter blocks a subtask.

    fallback ADR item 6 requires the deterministic guard to run *before* the agent
    is invoked ("do not generate then catch"), so this is raised ahead of the
    first generation call and ahead of the first progress event: no LLM
    invocation, no artifacts, no "생성 시작" progress that would be a lie.

    Signalled as an exception rather than a return value because
    ``execute``'s return type is the *success* bundle; folding a block into it
    would let a caller that forgets to check silently proceed.

    Carries the whole :class:`FallbackGateDecision` — never a flattened string
    — because the manual-review routing in the graph persists the original
    four fields, and ``reason_code`` in particular is owned by the rule table
    and must reach the audit record unmodified. ``prefilter_route_reason`` is a
    *separate* fixed value identifying which pipeline blocked with which
    verdict; it never overwrites ``decision.reason_code``.
    """

    stage = "prefilter"

    def __init__(
        self,
        message: str,
        *,
        decision: FallbackGateDecision,
        prefilter_route_reason: str,
    ) -> None:
        super().__init__(message)
        self.decision = decision
        self.prefilter_route_reason = prefilter_route_reason


class SessionStateError(OrchestratorError):
    """Raised when an operation is not legal in the current session state."""


class OrchestratorConfigError(OrchestratorError):
    """Raised when orchestrator wiring is misconfigured (R10 fail-fast)."""


__all__ = [
    "ApprovalGateNotFoundError",
    "ClarificationNotFoundError",
    "ClarificationRequiredError",
    "OrchestratorConfigError",
    "OrchestratorError",
    "PrefilterBlockedError",
    "SessionNotFoundError",
    "SessionStateError",
]
