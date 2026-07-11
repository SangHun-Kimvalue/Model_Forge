from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SubtaskKind = Literal["mechanical", "organic"]


class Subtask(BaseModel):
    """A single planner-emitted unit of work routed to a downstream module.

    Preconditions:
        id must be unique within a PlannerResponse. depends_on references other
        subtask ids in the same response; the planner adapter must not emit
        cycles.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    kind: SubtaskKind
    description: str = Field(min_length=1)
    depends_on: tuple[str, ...] = ()


class PlannerRequest(BaseModel):
    """Provider-neutral planner input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    trace_id: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class PlannerResponse(BaseModel):
    """Provider-neutral planner output.

    Postconditions:
        Either subtasks is non-empty, or clarifying_questions is non-empty
        (P4 — the planner must not silently invent subtasks when the prompt
        is ambiguous).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    subtasks: tuple[Subtask, ...] = ()
    clarifying_questions: tuple[str, ...] = ()
    trace_id: str | None = None

    @model_validator(mode="after")
    def validate_planner_contract(self) -> "PlannerResponse":
        if not self.subtasks and not self.clarifying_questions:
            raise ValueError("PlannerResponse requires subtasks or clarifying_questions.")
        if self.subtasks and self.clarifying_questions:
            raise ValueError(
                "PlannerResponse cannot mix subtasks and clarifying_questions."
            )

        ids = [subtask.id for subtask in self.subtasks]
        if len(ids) != len(set(ids)):
            raise ValueError("PlannerResponse subtask ids must be unique.")

        known_ids = set(ids)
        for subtask in self.subtasks:
            unknown_deps = set(subtask.depends_on) - known_ids
            if unknown_deps:
                raise ValueError(
                    f"Subtask '{subtask.id}' depends on unknown ids: "
                    f"{sorted(unknown_deps)}."
                )

        _assert_acyclic_dependencies(self.subtasks)
        return self


def _assert_acyclic_dependencies(subtasks: tuple[Subtask, ...]) -> None:
    graph = {subtask.id: set(subtask.depends_on) for subtask in subtasks}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            raise ValueError("PlannerResponse subtask dependencies must be acyclic.")
        visiting.add(node)
        for dep in graph[node]:
            visit(dep)
        visiting.remove(node)
        visited.add(node)

    for sid in graph:
        visit(sid)
