import hashlib

from modules.agents.planner.base import BasePlannerAgent
from modules.agents.planner.schemas import (
    PlannerRequest,
    PlannerResponse,
    Subtask,
    SubtaskKind,
)

_AMBIGUITY_MARKERS = ("뭔가", "something", "아무거나", "anything")
_MECHANICAL_ONLY_MARKERS = (
    "mechanical-only",
    "mechanical only",
    "기구부만",
    "g-code",
    "gcode",
    "지코드",
    "출력 파일",
)


class MockPlannerAgent(BasePlannerAgent):
    """Deterministic planner for unit tests and local scaffolding.

    Behavior is intentionally simple but exercises the contract:
      - prompts containing an ambiguity marker emit a clarifying question
        (P4 — never silently invent subtasks).
      - prompts containing an explicit mechanical-only marker emit one
        mechanical subtask so Phase 8A mock CAD → validator → slicer E2E
        can complete before the organic generator is wired.
      - otherwise emits exactly two subtasks (mechanical scaffold + organic
        detail) so downstream module wiring can be tested end-to-end.
    """

    default_adapter_name = "mock-planner-v1"

    def __init__(self, adapter_label: str | None = None) -> None:
        self._adapter_label = adapter_label or self.default_adapter_name

    @property
    def adapter_name(self) -> str:
        return self._adapter_label

    async def plan(self, request: PlannerRequest) -> PlannerResponse:
        if _is_ambiguous(request.prompt):
            return PlannerResponse(
                clarifying_questions=(
                    "프롬프트가 모호합니다. 만들고 싶은 대상의 용도와 대략 크기를 알려주세요.",
                ),
                trace_id=request.trace_id,
            )

        digest = _digest(request)
        if _is_mechanical_only(request.prompt):
            return PlannerResponse(
                subtasks=(
                    _subtask(f"mech-{digest}", "mechanical", request.prompt),
                ),
                trace_id=request.trace_id,
            )

        return PlannerResponse(
            subtasks=(
                _subtask(f"mech-{digest}", "mechanical", request.prompt),
                _subtask(
                    f"org-{digest}",
                    "organic",
                    request.prompt,
                    depends_on=(f"mech-{digest}",),
                ),
            ),
            trace_id=request.trace_id,
        )


def _is_ambiguous(prompt: str) -> bool:
    lowered = prompt.lower()
    return any(marker in lowered for marker in _AMBIGUITY_MARKERS)


def _is_mechanical_only(prompt: str) -> bool:
    lowered = prompt.lower()
    return any(marker in lowered for marker in _MECHANICAL_ONLY_MARKERS)


def _digest(request: PlannerRequest) -> str:
    payload = f"{request.session_id}\n{request.prompt}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def _subtask(
    sid: str,
    kind: SubtaskKind,
    prompt: str,
    *,
    depends_on: tuple[str, ...] = (),
) -> Subtask:
    role = "기구부" if kind == "mechanical" else "유기 형상"
    return Subtask(
        id=sid,
        kind=kind,
        description=f"{role}: {prompt}",
        depends_on=depends_on,
    )
