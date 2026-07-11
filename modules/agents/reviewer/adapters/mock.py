from modules.agents.reviewer.base import BaseReviewerAgent
from modules.agents.reviewer.schemas import (
    ReviewerRequest,
    ReviewerResponse,
    ReviewIssue,
)


class MockReviewerAgent(BaseReviewerAgent):
    """Deterministic reviewer for unit tests and local scaffolding.

    Default behavior is "pass with no issues" so downstream pipeline tests
    can exercise the happy path. Callers needing a failure case can construct
    the adapter with ``always_fail=True`` to emit a single error issue.
    """

    default_adapter_name = "mock-reviewer-v1"

    def __init__(
        self,
        adapter_label: str | None = None,
        *,
        always_fail: bool = False,
    ) -> None:
        self._adapter_label = adapter_label or self.default_adapter_name
        self._always_fail = always_fail

    @property
    def adapter_name(self) -> str:
        return self._adapter_label

    async def review(self, request: ReviewerRequest) -> ReviewerResponse:
        if self._always_fail:
            return ReviewerResponse(
                passed=False,
                issues=(
                    ReviewIssue(
                        code="MOCK_FAIL",
                        message=(
                            f"mock reviewer configured to fail review of "
                            f"{request.target_kind}:{request.payload_ref}"
                        ),
                        severity="error",
                    ),
                ),
                trace_id=request.trace_id,
            )
        return ReviewerResponse(passed=True, trace_id=request.trace_id)
