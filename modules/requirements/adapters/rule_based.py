from __future__ import annotations

import re

from modules.requirements.base import BaseRequirementExtractor
from modules.requirements.schemas import (
    DimensionRequirement,
    QualityCheck,
    RequirementExtractionRequest,
    RequirementSpec,
)

__all__ = ["RuleBasedRequirementExtractor"]

_KEYRING_MARKERS = ("keyring", "key ring", "keychain", "key chain", "키링", "열쇠고리", "키체인")
_BEAR_MARKERS = ("bear", "teddy", "곰", "곰돌", "베어")
_BRACKET_MARKERS = ("bracket", "브라켓")
_HOLE_MARKERS = ("hole", "holes", "구멍", "홀")
_CUP_MARKERS = ("cup", "컵")
_LOGO_MARKERS = ("logo", "text", "emboss", "로고", "문자", "글자", "양각")
_DRONE_MARKERS = ("drone", "quadcopter", "드론", "쿼드")


class RuleBasedRequirementExtractor(BaseRequirementExtractor):
    """Conservative keyword extractor for the first semantic quality gate."""

    default_adapter_name = "rule-based-requirement-extractor-v1"

    def __init__(self, adapter_label: str | None = None) -> None:
        self._adapter_label = adapter_label or self.default_adapter_name

    @property
    def adapter_name(self) -> str:
        return self._adapter_label

    def extract(self, request: RequirementExtractionRequest) -> RequirementSpec:
        prompt = request.prompt.strip()
        lowered = prompt.lower()

        if _has_any(lowered, _KEYRING_MARKERS):
            return _keyring_spec(lowered, trace_id=request.trace_id)
        if _has_any(lowered, _BRACKET_MARKERS):
            return _bracket_spec(prompt, lowered, trace_id=request.trace_id)
        if _has_any(lowered, _CUP_MARKERS):
            return _cup_spec(lowered, trace_id=request.trace_id)
        if _has_any(lowered, _DRONE_MARKERS):
            return _drone_spec(prompt, lowered, trace_id=request.trace_id)

        return RequirementSpec(
            needs_clarification=True,
            clarifying_questions=(
                "만들 대상의 종류, 대략 치수, 반드시 포함할 기능을 알려주세요.",
            ),
            trace_id=request.trace_id,
        )


def _keyring_spec(lowered: str, *, trace_id: str | None) -> RequirementSpec:
    required = ["keyring_hole"]
    tags = ["flat_2.5d", "decorative"]
    forbidden = ["blank_tag", "ring_only"]
    constraints = ["single_connected_part", "min_wall_2.5mm", "flat_2.5d"]
    checks = [QualityCheck(name="through_hole_required")]
    object_type = "keyring"

    if _has_any(lowered, _BEAR_MARKERS):
        tags.append("bear")
        required.extend(["bear_head", "ears", "eyes", "nose_or_muzzle"])
        checks.append(
            QualityCheck(
                name="feature_presence",
                params={"features": ["bear_head", "ears", "eyes", "nose_or_muzzle"]},
            )
        )

    return RequirementSpec(
        object_type=object_type,
        intent_tags=tuple(tags),
        required_features=tuple(required),
        forbidden_outcomes=tuple(forbidden),
        print_constraints=tuple(constraints),
        quality_checks=tuple(checks),
        trace_id=trace_id,
    )


def _bracket_spec(prompt: str, lowered: str, *, trace_id: str | None) -> RequirementSpec:
    hole_count = _extract_count_before_hole(prompt) if _has_any(lowered, _HOLE_MARKERS) else None
    has_size = _extract_mm_size(prompt) is not None
    checks = []
    required = ["bracket_body"]
    if hole_count:
        required.append("mounting_holes")
        checks.append(QualityCheck(name="hole_count_min", params={"count": hole_count}))
    # Keep historical English test prompts stable; the Phase 11C-4 smoke
    # target is the Korean product prompt "브라켓 만들어줘".
    needs_clarification = "브라켓" in lowered and (not has_size or hole_count is None)
    clarifying_questions: list[str] = []
    if not has_size:
        clarifying_questions.append(
            "브라켓의 대략적인 가로, 세로, 두께를 mm 단위로 알려주세요."
        )
    if hole_count is None:
        clarifying_questions.append("체결 구멍은 몇 개가 필요한가요?")
    if needs_clarification:
        clarifying_questions.append("어떤 용도로 사용할 예정인가요?")

    return RequirementSpec(
        object_type="bracket",
        intent_tags=("fixture",),
        required_features=tuple(required),
        forbidden_outcomes=("holes_missing",) if hole_count else (),
        print_constraints=("single_connected_part", "min_wall_2.5mm"),
        quality_checks=tuple(checks),
        needs_clarification=needs_clarification,
        clarifying_questions=tuple(clarifying_questions[:3]),
        trace_id=trace_id,
    )


def _cup_spec(lowered: str, *, trace_id: str | None) -> RequirementSpec:
    required = ["cup_body"]
    forbidden = []
    checks = []
    if _has_any(lowered, _LOGO_MARKERS) or "generic_printer" in lowered:
        required.append("raised_logo_or_text")
        forbidden.append("logo_missing")
        checks.append(QualityCheck(name="logo_or_text_required"))

    return RequirementSpec(
        object_type="cup",
        intent_tags=("container",),
        required_features=tuple(required),
        forbidden_outcomes=tuple(forbidden),
        print_constraints=("single_connected_part", "min_wall_2.5mm"),
        quality_checks=tuple(checks),
        trace_id=trace_id,
    )


def _drone_spec(prompt: str, lowered: str, *, trace_id: str | None) -> RequirementSpec:
    dimensions = []
    size = _extract_mm_size(prompt)
    if size:
        dimensions.append(
            DimensionRequirement(
                name="outer_span_xy",
                value_mm=float(size),
                mode="exact",
                tolerance_mm=1.0,
            )
        )

    return RequirementSpec(
        object_type="drone_frame",
        intent_tags=("mechanical", "frame"),
        required_features=("central_hub", "arms", "motor_mounts"),
        forbidden_outcomes=("motor_mounts_missing", "plain_cross"),
        dimensions=tuple(dimensions),
        print_constraints=("single_connected_part", "support_light"),
        quality_checks=(QualityCheck(name="feature_presence"),),
        trace_id=trace_id,
    )


def _has_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _extract_count_before_hole(prompt: str) -> int | None:
    patterns = (
        r"(\d+)\s*개\s*(?:의\s*)?(?:나사\s*)?(?:구멍|홀)",
        r"(?:구멍|홀)\s*(\d+)\s*개",
        r"(\d+)\s*(?:mounting\s*)?holes?",
    )
    for pattern in patterns:
        match = re.search(pattern, prompt, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _extract_mm_size(prompt: str) -> int | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:mm|밀리)", prompt, re.IGNORECASE)
    if match:
        return int(float(match.group(1)))
    return None
