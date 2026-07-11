from abc import ABC, abstractmethod

from modules.agents.reviewer.schemas import ReviewerRequest, ReviewerResponse


class BaseReviewerAgent(ABC):
    """Contract for pre-slice quality / contract review.

    Preconditions:
        request.payload_ref must resolve to an artifact the adapter can read.
    Postconditions:
        response.passed is False whenever any issue has severity 'error'.
    Raises:
        ReviewerAgentError subclasses on configuration or review failure.
    """

    @property
    @abstractmethod
    def adapter_name(self) -> str:
        """Stable adapter identifier used in logs, traces, and tests."""

    @abstractmethod
    async def review(self, request: ReviewerRequest) -> ReviewerResponse:
        """Review the referenced artifact against the given criteria."""
