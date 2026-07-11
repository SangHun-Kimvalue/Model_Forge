from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ReviewTargetKind = Literal["cad_code", "mesh", "slicer_input"]
ReviewSeverity = Literal["info", "warning", "error"]


class ReviewIssue(BaseModel):
    """A single reviewer-emitted observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    severity: ReviewSeverity = "warning"


class ReviewerRequest(BaseModel):
    """Provider-neutral reviewer input.

    Preconditions:
        payload_ref is an opaque locator (file path, artifact id) the reviewer
        adapter knows how to resolve. The agent contract does not embed the
        full payload to keep request size bounded (P12).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_kind: ReviewTargetKind
    payload_ref: str = Field(min_length=1)
    criteria: tuple[str, ...] = ()
    context: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None


class ReviewerResponse(BaseModel):
    """Provider-neutral reviewer output.

    Postconditions:
        passed is False when any issue has severity 'error'. Adapters that
        cannot enforce this invariant must raise instead of returning a
        contradictory response.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    issues: tuple[ReviewIssue, ...] = ()
    trace_id: str | None = None

    @model_validator(mode="after")
    def validate_reviewer_contract(self) -> "ReviewerResponse":
        if self.passed and any(issue.severity == "error" for issue in self.issues):
            raise ValueError("ReviewerResponse cannot pass with error severity issues.")
        return self
