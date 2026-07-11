from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from modules.requirements.schemas import RequirementSpec

CADDsl = Literal["cadquery", "build123d", "openscad"]


class CADCoderRequest(BaseModel):
    """Provider-neutral CAD coder input.

    Preconditions:
        subtask_id should be the id emitted by the planner for traceability.
        description is the mechanical-only portion of the prompt; organic
        details belong to the organic_generator pipeline.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    subtask_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    dsl: CADDsl = "cadquery"
    constraints: dict[str, Any] = Field(default_factory=dict)
    requirements: RequirementSpec | None = None
    trace_id: str | None = None


class CADCoderResponse(BaseModel):
    """Provider-neutral CAD coder output.

    Postconditions:
        code is the full source for a single self-contained script in the
        requested dsl. entrypoint names the callable / variable the sandbox
        runner is expected to invoke / read.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    subtask_id: str = Field(min_length=1)
    dsl: CADDsl
    code: str = Field(min_length=1)
    entrypoint: str = Field(min_length=1, default="main")
    trace_id: str | None = None
