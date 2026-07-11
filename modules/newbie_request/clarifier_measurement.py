"""Phase 12Z-B2 clarifier measurement fixture and aggregate helpers.

These helpers validate the offline measurement fixture and post-process human
labels. They do not call an LLM and they do not affect product routing.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_CLARIFIER_CASES_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs/llm_benchmark/12z_b_clarifier_cases.json"
)
TRADEMARK_REASON = "trademark_blocked_subject"


class ClarifierHumanLabel(StrEnum):
    """Human-only quality label for a measured clarification question."""

    MORE_SPECIFIC = "more_specific"
    EQUIVALENT = "equivalent"
    WORSE = "worse"
    HALLUCINATED = "hallucinated"
    NOT_LABELED = "not_labeled"


class ClarifierPromotionThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    non_trademark_more_specific_min_rate: float = Field(ge=0, le=1)
    hallucinated_max_count: int = Field(ge=0)
    trademark_llm_call_count: int = Field(ge=0)


class ClarifierMeasurementCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    deterministic_reason: str = Field(min_length=1)
    deterministic_questions: tuple[str, ...] = Field(min_length=1)
    llm_call_allowed: bool
    expected_labeling_policy: str = Field(min_length=1)

    @property
    def is_trademark_case(self) -> bool:
        return self.deterministic_reason == TRADEMARK_REASON


class ClarifierMeasurementFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(min_length=1)
    phase: str = Field(min_length=1)
    promotion_thresholds: ClarifierPromotionThresholds
    labels: tuple[ClarifierHumanLabel, ...]
    cases: tuple[ClarifierMeasurementCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_fixture(self) -> ClarifierMeasurementFixture:
        expected_labels = tuple(ClarifierHumanLabel)
        if tuple(self.labels) != expected_labels:
            raise ValueError(
                "clarifier fixture labels must match the supported human label set"
            )
        seen_case_ids: set[str] = set()
        for case in self.cases:
            if case.case_id in seen_case_ids:
                raise ValueError(f"duplicate case_id: {case.case_id}")
            seen_case_ids.add(case.case_id)
            if case.is_trademark_case and case.llm_call_allowed:
                raise ValueError(
                    "trademark clarifier cases must set llm_call_allowed=false"
                )
        return self


def load_clarifier_measurement_fixture(
    path: Path = DEFAULT_CLARIFIER_CASES_PATH,
) -> ClarifierMeasurementFixture:
    """Load and validate the frozen Phase 12Z-B2 measurement fixture."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    return ClarifierMeasurementFixture.model_validate(payload)


def compute_human_label_aggregate(
    case_results: list[dict[str, Any]],
    thresholds: ClarifierPromotionThresholds,
) -> dict[str, Any]:
    """Aggregate human labels only when all non-trademark cases are labeled.

    The runner must not fabricate ``more_specific``/``worse`` labels. Until a
    human labels each non-trademark case, promotion-threshold calculation is
    intentionally unavailable.
    """

    invalid_label_case_ids: list[str] = []
    normalized_results: list[tuple[dict[str, Any], ClarifierHumanLabel | None]] = []
    for result in case_results:
        label = result.get("human_label")
        if not isinstance(label, str):
            normalized_label = None
            invalid_label_case_ids.append(str(result.get("case_id") or "<unknown>"))
            normalized_results.append((result, normalized_label))
            continue
        try:
            normalized_label = ClarifierHumanLabel(label)
        except ValueError:
            normalized_label = None
            invalid_label_case_ids.append(str(result.get("case_id") or "<unknown>"))
        normalized_results.append((result, normalized_label))

    non_trademark = [
        (result, label)
        for result, label in normalized_results
        if result.get("deterministic_reason") != TRADEMARK_REASON
    ]
    unlabeled = [
        result.get("case_id")
        for result, label in non_trademark
        if label is ClarifierHumanLabel.NOT_LABELED or label is None
    ]
    trademark_call_count = sum(
        int(result.get("call_count") or 0)
        for result, _label in normalized_results
        if result.get("deterministic_reason") == TRADEMARK_REASON
    )
    if invalid_label_case_ids:
        return {
            "threshold_calculable": False,
            "reason": "invalid_human_labels",
            "unlabeled_case_ids": unlabeled,
            "invalid_label_case_ids": invalid_label_case_ids,
            "trademark_call_count": trademark_call_count,
            "trademark_call_count_ok": (
                trademark_call_count <= thresholds.trademark_llm_call_count
            ),
            "more_specific_rate": None,
            "hallucinated_count": None,
            "promotion_candidate_signal": "not_calculable_without_valid_human_labels",
        }
    if unlabeled:
        return {
            "threshold_calculable": False,
            "reason": "human_labels_missing",
            "unlabeled_case_ids": unlabeled,
            "invalid_label_case_ids": invalid_label_case_ids,
            "trademark_call_count": trademark_call_count,
            "trademark_call_count_ok": (
                trademark_call_count <= thresholds.trademark_llm_call_count
            ),
            "more_specific_rate": None,
            "hallucinated_count": None,
            "promotion_candidate_signal": "not_calculable_without_human_labels",
        }

    denominator = len(non_trademark)
    more_specific_count = sum(
        1
        for _result, label in non_trademark
        if label is ClarifierHumanLabel.MORE_SPECIFIC
    )
    hallucinated_count = sum(
        1
        for _result, label in non_trademark
        if label is ClarifierHumanLabel.HALLUCINATED
    )
    more_specific_rate = (
        more_specific_count / denominator if denominator else None
    )
    threshold_ok = bool(
        more_specific_rate is not None
        and more_specific_rate
        >= thresholds.non_trademark_more_specific_min_rate
        and hallucinated_count <= thresholds.hallucinated_max_count
        and trademark_call_count <= thresholds.trademark_llm_call_count
    )
    return {
        "threshold_calculable": True,
        "reason": None,
        "unlabeled_case_ids": [],
        "invalid_label_case_ids": [],
        "non_trademark_case_count": denominator,
        "more_specific_count": more_specific_count,
        "more_specific_rate": more_specific_rate,
        "hallucinated_count": hallucinated_count,
        "trademark_call_count": trademark_call_count,
        "trademark_call_count_ok": (
            trademark_call_count <= thresholds.trademark_llm_call_count
        ),
        "promotion_candidate_signal": (
            "reference_signal_met" if threshold_ok else "reference_signal_not_met"
        ),
    }
