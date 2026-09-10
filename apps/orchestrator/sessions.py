"""Session store + in-memory implementation (DESIGN.md §4.2).

The orchestrator owns session lifecycle state — created → planning →
awaiting_approval → running → completed/failed. ``SessionStore`` is an
ABC so a Redis-backed store can be swapped in later (P10 — current
needs do not justify Redis yet; PHASES Phase 6 explicitly defers it).

Each ``Session`` carries:
- a unique id and a Phase-3 ``trace_id`` (logs and events tag both),
- an asyncio event queue per session for the WS subscriber,
- registered ``ApprovalGate`` instances (P9 — human-in-the-loop),
- a snapshot of planner-emitted subtasks.

Mutations go through the store; the store guards concurrent access
with an ``asyncio.Lock`` so /chat and /approve cannot race on the same
session.
"""

import asyncio
import hashlib
import json
import secrets
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime

from apps.orchestrator.exceptions import (
    ApprovalGateNotFoundError,
    ClarificationNotFoundError,
    SessionNotFoundError,
    SessionStateError,
)
from apps.orchestrator.schemas import (
    ApprovalDecision,
    ApprovalStatus,
    ClarificationAnswerValue,
    ClarificationQuestion,
    ClarificationStatus,
    IntakeChoiceResponse,
    IntakeNextStepView,
    NaturalLanguageRouteView,
    PendingClarificationView,
    PendingGateView,
    SessionEvent,
    SessionEventKind,
    SessionState,
    SessionStateView,
    SubtaskView,
)

_EVENT_QUEUE_MAX = 256


def _now() -> datetime:
    return datetime.now(UTC)


def _new_session_id() -> str:
    return f"sess_{uuid.uuid4().hex[:12]}"


def _new_trace_id() -> str:
    return f"trace_{secrets.token_hex(8)}"


def _new_gate_id() -> str:
    return f"gate_{uuid.uuid4().hex[:10]}"


def _new_clarification_id() -> str:
    return f"clar_{uuid.uuid4().hex[:10]}"


@dataclass
class ApprovalGate:
    """A single human-in-the-loop checkpoint awaiting decision (P9).

    The gate owns the exact subtask snapshot the user approved. Execution must
    read from the gate, not from the session's mutable current view.
    """

    gate_id: str
    prompt: str
    requested_at: datetime
    subtasks: tuple[SubtaskView, ...]
    plan_snapshot_id: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    decision: ApprovalDecision | None = None
    resolved_at: datetime | None = None
    comments: str | None = None

    def to_view(self) -> PendingGateView:
        return PendingGateView(
            gate_id=self.gate_id,
            prompt=self.prompt,
            requested_at=self.requested_at,
            status=self.status,
        )


@dataclass
class ClarificationGate:
    """A short ask-user gate, separate from approval gates."""

    clarification_id: str
    session_id: str
    subtask_id: str | None
    requested_at: datetime
    questions: tuple[ClarificationQuestion, ...]
    source_decision: dict[str, object]
    reason: str
    resume_subtasks: tuple[SubtaskView, ...]
    plan_snapshot_id: str | None
    requirement_snapshot_id: str
    approval_gate_id: str | None = None
    status: ClarificationStatus = ClarificationStatus.PENDING
    answered_at: datetime | None = None
    answers: dict[str, ClarificationAnswerValue] = field(default_factory=dict)

    def to_view(self, trace_id: str | None) -> PendingClarificationView:
        return PendingClarificationView(
            clarification_id=self.clarification_id,
            session_id=self.session_id,
            subtask_id=self.subtask_id,
            trace_id=trace_id,
            reason=self.reason,
            questions=self.questions,
            source_decision=self.source_decision,
            plan_snapshot_id=self.plan_snapshot_id,
            requirement_snapshot_id=self.requirement_snapshot_id,
            requested_at=self.requested_at,
            status=self.status,
        )


@dataclass
class Session:
    """In-memory representation of an orchestrator session."""

    session_id: str
    trace_id: str
    created_at: datetime
    state: SessionState = SessionState.CREATED
    gates: dict[str, ApprovalGate] = field(default_factory=dict)
    clarifications: dict[str, ClarificationGate] = field(default_factory=dict)
    pending_clarification_id: str | None = None
    subtasks: tuple[SubtaskView, ...] = ()
    route_revision: int = 0
    latest_route_result: NaturalLanguageRouteView | None = None
    latest_intake_choice: IntakeChoiceResponse | None = None
    latest_intake_next_step: IntakeNextStepView | None = None
    event_queues: list[asyncio.Queue[SessionEvent]] = field(default_factory=list)

    def to_view(self) -> SessionStateView:
        pending = tuple(
            g.to_view()
            for g in self.gates.values()
            if g.status is ApprovalStatus.PENDING
        )
        return SessionStateView(
            session_id=self.session_id,
            trace_id=self.trace_id,
            state=self.state,
            created_at=self.created_at,
            pending_gates=pending,
            pending_clarification=pending_clarification_view(self),
            subtasks=self.subtasks,
            latest_route_result=self.latest_route_result,
            latest_intake_choice=self.latest_intake_choice,
            latest_intake_next_step=self.latest_intake_next_step,
        )


class SessionStore(ABC):
    """Abstract store; concrete impls handle storage + locking."""

    @abstractmethod
    async def create(self) -> Session: ...

    @abstractmethod
    async def get(self, session_id: str) -> Session: ...

    @abstractmethod
    async def list_ids(self) -> tuple[str, ...]: ...

    @abstractmethod
    def lock_for(self, session_id: str) -> asyncio.Lock: ...

    @abstractmethod
    async def publish(self, session_id: str, event: SessionEvent) -> None: ...

    @abstractmethod
    async def subscribe(self, session_id: str) -> asyncio.Queue[SessionEvent]: ...

    @abstractmethod
    async def unsubscribe(
        self, session_id: str, queue: asyncio.Queue[SessionEvent]
    ) -> None: ...


class InMemorySessionStore(SessionStore):
    """Default store used by tests and the default app factory.

    Lock scope is per-session so multiple sessions can progress
    concurrently. The store-level mutex only protects insertion/lookup
    of the session map itself.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._store_mu = asyncio.Lock()

    async def create(self) -> Session:
        async with self._store_mu:
            session = Session(
                session_id=_new_session_id(),
                trace_id=_new_trace_id(),
                created_at=_now(),
            )
            self._sessions[session.session_id] = session
            self._locks[session.session_id] = asyncio.Lock()
            return session

    async def get(self, session_id: str) -> Session:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise SessionNotFoundError(
                f"Session '{session_id}' does not exist."
            ) from exc

    async def list_ids(self) -> tuple[str, ...]:
        return tuple(self._sessions.keys())

    def lock_for(self, session_id: str) -> asyncio.Lock:
        try:
            return self._locks[session_id]
        except KeyError as exc:
            raise SessionNotFoundError(
                f"Session '{session_id}' does not exist."
            ) from exc

    async def publish(self, session_id: str, event: SessionEvent) -> None:
        session = await self.get(session_id)
        for queue in list(session.event_queues):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # WS subscriber too slow — drop the oldest event and retry once.
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except asyncio.QueueEmpty:  # pragma: no cover — defensive
                    pass

    async def subscribe(self, session_id: str) -> asyncio.Queue[SessionEvent]:
        session = await self.get(session_id)
        queue: asyncio.Queue[SessionEvent] = asyncio.Queue(maxsize=_EVENT_QUEUE_MAX)
        session.event_queues.append(queue)
        return queue

    async def unsubscribe(
        self, session_id: str, queue: asyncio.Queue[SessionEvent]
    ) -> None:
        session = await self.get(session_id)
        try:
            session.event_queues.remove(queue)
        except ValueError:
            # Already removed — idempotent disconnect.
            return


def make_event(
    session: Session,
    kind: SessionEventKind,
    *,
    payload: dict[str, object] | None = None,
) -> SessionEvent:
    """Helper that fills the common fields (session_id, trace_id, state)."""
    return SessionEvent(
        session_id=session.session_id,
        trace_id=session.trace_id,
        kind=kind,
        state=session.state,
        occurred_at=_now(),
        payload=dict(payload or {}),
    )


def transition(
    session: Session,
    target: SessionState,
    *,
    allowed_from: tuple[SessionState, ...],
) -> None:
    """Move a session to ``target`` only from one of ``allowed_from``.

    Raises:
        SessionStateError when the current state is not in ``allowed_from``.
    """
    if session.state not in allowed_from:
        raise SessionStateError(
            f"Session '{session.session_id}' cannot move from "
            f"{session.state.value!r} to {target.value!r}."
        )
    session.state = target


def register_gate(
    session: Session,
    prompt: str,
    *,
    subtasks: tuple[SubtaskView, ...],
) -> ApprovalGate:
    gate = ApprovalGate(
        gate_id=_new_gate_id(),
        prompt=prompt,
        requested_at=_now(),
        subtasks=subtasks,
        plan_snapshot_id=plan_snapshot_id_for(subtasks),
    )
    session.gates[gate.gate_id] = gate
    return gate


def pending_clarification_view(session: Session) -> PendingClarificationView | None:
    clarification_id = session.pending_clarification_id
    if clarification_id is None:
        return None
    gate = session.clarifications.get(clarification_id)
    if gate is None or gate.status is not ClarificationStatus.PENDING:
        return None
    return gate.to_view(session.trace_id)


def register_clarification(
    session: Session,
    *,
    subtask: SubtaskView,
    resume_subtasks: tuple[SubtaskView, ...],
    questions: tuple[ClarificationQuestion, ...],
    source_decision: dict[str, object],
    reason: str,
    plan_snapshot_id: str | None,
    approval_gate_id: str | None,
) -> ClarificationGate:
    if len(questions) < 1 or len(questions) > 3:
        raise SessionStateError("Clarification requires between 1 and 3 questions.")
    gate = ClarificationGate(
        clarification_id=_new_clarification_id(),
        session_id=session.session_id,
        subtask_id=subtask.id,
        requested_at=_now(),
        questions=questions,
        source_decision=source_decision,
        reason=reason,
        resume_subtasks=resume_subtasks,
        plan_snapshot_id=plan_snapshot_id,
        requirement_snapshot_id=requirement_snapshot_id_for(
            subtask,
            questions,
            source_decision,
        ),
        approval_gate_id=approval_gate_id,
    )
    session.clarifications[gate.clarification_id] = gate
    session.pending_clarification_id = gate.clarification_id
    return gate


def register_route_clarification(
    session: Session,
    *,
    questions: tuple[ClarificationQuestion, ...],
    source_decision: dict[str, object],
    reason: str,
) -> ClarificationGate:
    gate = prepare_route_clarification(
        session,
        questions=questions,
        source_decision=source_decision,
        reason=reason,
    )
    commit_route_clarification(session, gate)
    return gate


def prepare_route_clarification(
    session: Session,
    *,
    questions: tuple[ClarificationQuestion, ...],
    source_decision: dict[str, object],
    reason: str,
) -> ClarificationGate:
    """Build a route gate without mutating the session."""
    if len(questions) < 1 or len(questions) > 3:
        raise SessionStateError("Clarification requires between 1 and 3 questions.")
    return ClarificationGate(
        clarification_id=_new_clarification_id(),
        session_id=session.session_id,
        subtask_id=None,
        requested_at=_now(),
        questions=questions,
        source_decision=source_decision,
        reason=reason,
        resume_subtasks=(),
        plan_snapshot_id=None,
        requirement_snapshot_id=route_requirement_snapshot_id_for(
            questions,
            source_decision,
        ),
        approval_gate_id=None,
    )


def commit_route_clarification(session: Session, gate: ClarificationGate) -> None:
    """Commit a previously prepared route gate."""
    session.clarifications[gate.clarification_id] = gate
    session.pending_clarification_id = gate.clarification_id


def plan_snapshot_id_for(subtasks: tuple[SubtaskView, ...]) -> str:
    """Return a stable hash of the exact subtask snapshot being approved."""
    payload = [st.model_dump(mode="json") for st in subtasks]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def requirement_snapshot_id_for(
    subtask: SubtaskView,
    questions: tuple[ClarificationQuestion, ...],
    source_decision: dict[str, object],
) -> str:
    payload = {
        "subtask": subtask.model_dump(mode="json"),
        "questions": [q.model_dump(mode="json") for q in questions],
        "source_decision": source_decision,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def route_requirement_snapshot_id_for(
    questions: tuple[ClarificationQuestion, ...],
    source_decision: dict[str, object],
) -> str:
    payload = {
        "scope": "natural_language_route",
        "questions": [q.model_dump(mode="json") for q in questions],
        "source_decision": source_decision,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def resolve_gate(
    session: Session,
    gate_id: str,
    decision: ApprovalDecision,
    comments: str | None,
) -> ApprovalGate:
    try:
        gate = session.gates[gate_id]
    except KeyError as exc:
        raise ApprovalGateNotFoundError(
            f"Gate '{gate_id}' is not registered on session '{session.session_id}'."
        ) from exc
    if gate.status is not ApprovalStatus.PENDING:
        raise ApprovalGateNotFoundError(
            f"Gate '{gate_id}' is already resolved (status={gate.status.value})."
        )
    gate.status = (
        ApprovalStatus.APPROVED
        if decision is ApprovalDecision.APPROVED
        else ApprovalStatus.REJECTED
    )
    gate.decision = decision
    gate.resolved_at = _now()
    gate.comments = comments
    return gate


def resolve_clarification(
    session: Session,
    clarification_id: str,
    answers: dict[str, ClarificationAnswerValue],
) -> ClarificationGate:
    gate = prepare_clarification_resolution(session, clarification_id, answers)
    gate.status = ClarificationStatus.ANSWERED
    gate.answered_at = _now()
    gate.answers = dict(answers)
    session.pending_clarification_id = None
    return gate


def prepare_clarification_resolution(
    session: Session,
    clarification_id: str,
    answers: dict[str, ClarificationAnswerValue],
) -> ClarificationGate:
    """Validate a clarification answer without changing gate or session state."""
    try:
        gate = session.clarifications[clarification_id]
    except KeyError as exc:
        raise ClarificationNotFoundError(
            f"Clarification '{clarification_id}' is not registered on "
            f"session '{session.session_id}'."
        ) from exc
    if gate.status is not ClarificationStatus.PENDING:
        raise ClarificationNotFoundError(
            f"Clarification '{clarification_id}' is already resolved "
            f"(status={gate.status.value})."
        )
    if session.pending_clarification_id != clarification_id:
        raise ClarificationNotFoundError(
            f"Clarification '{clarification_id}' is stale for session "
            f"'{session.session_id}'."
        )
    expected_ids = {q.id for q in gate.questions if q.required}
    missing = tuple(sorted(expected_ids.difference(answers.keys())))
    if missing:
        raise SessionStateError(
            "Clarification answers are missing required question id(s): "
            + ", ".join(missing)
            + "."
        )
    return gate


__all__ = [
    "ApprovalGate",
    "ClarificationGate",
    "InMemorySessionStore",
    "Session",
    "SessionStore",
    "make_event",
    "pending_clarification_view",
    "prepare_clarification_resolution",
    "prepare_route_clarification",
    "plan_snapshot_id_for",
    "register_clarification",
    "register_route_clarification",
    "commit_route_clarification",
    "register_gate",
    "requirement_snapshot_id_for",
    "route_requirement_snapshot_id_for",
    "resolve_clarification",
    "resolve_gate",
    "transition",
]
