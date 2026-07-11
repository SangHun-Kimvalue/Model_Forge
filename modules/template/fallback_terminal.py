"""Deterministic terminal-state decision for template fallback misses."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class TerminalState(StrEnum):
    """Terminal action after template miss and fallback gate evaluation."""

    ASK_USER = "ask_user"
    MANUAL_REVIEW = "manual_review"
    BOUNDED_REGENERATE = "bounded_regenerate"
    GIVE_UP = "give_up"


class FallbackTerminalDecision(BaseModel):
    """Frozen decision payload for fallback terminal handling."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: TerminalState
    reason_code: str
    user_message_ko: str
    may_retry: bool


_VALID_PREFILTER_DECISIONS = {"allow", "blocked_prohibited", "manual_review_ip"}


def decide_fallback_terminal_state(
    *,
    prefilter_decision: str,
    gate_failed: bool,
    attempts: int,
    max_attempts: int = 2,
    cost_spent: float = 0.0,
    cost_cap: float | None = None,
) -> FallbackTerminalDecision:
    """Decide the terminal fallback state without invoking LLMs or routers."""

    _validate_inputs(
        prefilter_decision=prefilter_decision,
        attempts=attempts,
        max_attempts=max_attempts,
        cost_spent=cost_spent,
        cost_cap=cost_cap,
    )

    if prefilter_decision == "blocked_prohibited":
        return FallbackTerminalDecision(
            state=TerminalState.GIVE_UP,
            reason_code="prefilter_blocked_prohibited",
            user_message_ko=(
                "요청이 금지된 내용으로 분류되어 자동 재생성을 중단합니다. "
                "허용 가능한 범위로 요청을 바꿔 주세요."
            ),
            may_retry=False,
        )

    if prefilter_decision == "manual_review_ip":
        return FallbackTerminalDecision(
            state=TerminalState.MANUAL_REVIEW,
            reason_code="prefilter_manual_review_ip",
            user_message_ko=(
                "지식재산권 검토가 필요한 요청으로 분류되어 자동 재생성을 중단합니다. "
                "사람 검토 후 진행해 주세요."
            ),
            may_retry=False,
        )

    if gate_failed and attempts < max_attempts and _within_cost_cap(cost_spent, cost_cap):
        return FallbackTerminalDecision(
            state=TerminalState.BOUNDED_REGENERATE,
            reason_code="gate_failed_retry_budget_available",
            user_message_ko=(
                "자동 생성 결과가 품질 관문을 통과하지 못했습니다. "
                "제한된 횟수 안에서 한 번 더 재생성합니다."
            ),
            may_retry=True,
        )

    if gate_failed and attempts >= max_attempts:
        reason_code = "attempt_cap_reached"
        user_message_ko = (
            "자동 재생성 한도에 도달했습니다. "
            "추가 비용 발생을 막기 위해 사용자 확인이 필요합니다."
        )
    elif gate_failed and not _within_cost_cap(cost_spent, cost_cap):
        reason_code = "cost_cap_reached"
        user_message_ko = (
            "자동 재생성 비용 한도에 도달했습니다. "
            "추가 비용 발생을 막기 위해 사용자 확인이 필요합니다."
        )
    else:
        reason_code = "unresolved_without_gate_failure"
        user_message_ko = (
            "템플릿 매칭과 자동 처리로 해결되지 않았습니다. "
            "진행 방향을 사용자에게 확인해야 합니다."
        )

    return FallbackTerminalDecision(
        state=TerminalState.ASK_USER,
        reason_code=reason_code,
        user_message_ko=user_message_ko,
        may_retry=False,
    )


def _within_cost_cap(cost_spent: float, cost_cap: float | None) -> bool:
    if cost_cap is None:
        return True
    return cost_spent < cost_cap


def _validate_inputs(
    *,
    prefilter_decision: str,
    attempts: int,
    max_attempts: int,
    cost_spent: float,
    cost_cap: float | None,
) -> None:
    if prefilter_decision not in _VALID_PREFILTER_DECISIONS:
        raise ValueError(f"unknown prefilter_decision: {prefilter_decision!r}")
    if attempts < 0:
        raise ValueError("attempts must be greater than or equal to 0")
    if max_attempts < 0:
        raise ValueError("max_attempts must be greater than or equal to 0")
    if cost_spent < 0:
        raise ValueError("cost_spent must be greater than or equal to 0")
    if cost_cap is not None and cost_cap < 0:
        raise ValueError("cost_cap must be greater than or equal to 0")
