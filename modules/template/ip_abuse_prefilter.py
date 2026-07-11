from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from modules.newbie_request.subject_aliases import (
    TrademarkBlockedSubjectTable,
    load_trademark_blocked_subjects,
    normalize_alias_term,
)

Decision = Literal["allow", "blocked_prohibited", "manual_review_ip"]

DEFAULT_PROHIBITED_CATEGORIES_PATH = (
    Path(__file__).resolve().parent / "data" / "prohibited_categories.json"
)
DEFAULT_TRADEMARK_BLOCKED_SUBJECTS_PATH = (
    Path(__file__).resolve().parents[1]
    / "newbie_request"
    / "data"
    / "trademark_blocked_subjects.json"
)


class FallbackGateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Decision
    matched_rule_id: str | None
    reason_code: str
    user_message_ko: str


class ProhibitedCategory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category_id: str
    terms: tuple[str, ...] = Field(min_length=1)
    reason: Literal["prohibited_category"] = "prohibited_category"
    user_message_ko: str

    @model_validator(mode="after")
    def _terms_must_be_unique_after_normalization(self) -> ProhibitedCategory:
        seen: set[str] = set()
        for term in self.terms:
            normalized = normalize_alias_term(term)
            if not normalized:
                raise ValueError(f"empty prohibited term in category {self.category_id!r}")
            if normalized in seen:
                raise ValueError(
                    f"duplicate normalized prohibited term {normalized!r} "
                    f"in category {self.category_id!r}"
                )
            seen.add(normalized)
        return self


class ProhibitedCategoriesFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["12z-g.prohibited-categories.v1"]
    categories: tuple[ProhibitedCategory, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _terms_must_be_unique_after_normalization(self) -> ProhibitedCategoriesFile:
        owner_by_term: dict[str, str] = {}
        for category in self.categories:
            for term in category.terms:
                normalized = normalize_alias_term(term)
                existing_owner = owner_by_term.get(normalized)
                if existing_owner is not None:
                    raise ValueError(
                        f"duplicate normalized prohibited term {normalized!r}: "
                        f"{existing_owner!r} and {category.category_id!r}"
                    )
                owner_by_term[normalized] = category.category_id
        return self


class ProhibitedCategoryTable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    categories: tuple[ProhibitedCategory, ...]

    def match(self, prompt: str) -> ProhibitedCategory | None:
        normalized_prompt = normalize_alias_term(prompt)
        if not normalized_prompt:
            return None

        for category in self.categories:
            for term in category.terms:
                if normalize_alias_term(term) in normalized_prompt:
                    return category
        return None


def load_prohibited_categories(
    path: str | Path = DEFAULT_PROHIBITED_CATEGORIES_PATH,
) -> ProhibitedCategoryTable:
    categories_file = ProhibitedCategoriesFile.model_validate_json(
        Path(path).read_text(encoding="utf-8")
    )
    return ProhibitedCategoryTable(categories=categories_file.categories)


@lru_cache(maxsize=1)
def _default_prohibited_table() -> ProhibitedCategoryTable:
    return load_prohibited_categories(DEFAULT_PROHIBITED_CATEGORIES_PATH)


@lru_cache(maxsize=1)
def _default_trademark_table() -> TrademarkBlockedSubjectTable:
    return load_trademark_blocked_subjects(DEFAULT_TRADEMARK_BLOCKED_SUBJECTS_PATH)


def should_invoke_fallback_agent(
    prompt: str,
    *,
    trademark_table: TrademarkBlockedSubjectTable | None = None,
    prohibited_table: ProhibitedCategoryTable | None = None,
) -> FallbackGateDecision:
    """Decide whether fallback generation may run before any generation is attempted."""

    active_prohibited_table = (
        prohibited_table if prohibited_table is not None else _default_prohibited_table()
    )
    prohibited_match = active_prohibited_table.match(prompt)
    if prohibited_match is not None:
        return FallbackGateDecision(
            decision="blocked_prohibited",
            matched_rule_id=prohibited_match.category_id,
            reason_code=prohibited_match.reason,
            user_message_ko=prohibited_match.user_message_ko,
        )

    active_trademark_table = (
        trademark_table if trademark_table is not None else _default_trademark_table()
    )
    trademark_match = active_trademark_table.match(prompt)
    if trademark_match is not None:
        return FallbackGateDecision(
            decision="manual_review_ip",
            matched_rule_id=_extract_string(trademark_match, "subject"),
            reason_code=(
                _extract_string(trademark_match, "reason")
                or "trademark_or_ip_review_required"
            ),
            user_message_ko=_first_question(trademark_match)
            or "상표/IP 검토가 필요한 요청입니다. 자체 디자인으로 바꿀지 확인해 주세요.",
        )

    return FallbackGateDecision(
        decision="allow", matched_rule_id=None, reason_code="", user_message_ko=""
    )


def _extract_string(value: object, field_name: str) -> str | None:
    raw = getattr(value, field_name, None)
    if raw is None and isinstance(value, dict):
        raw = value.get(field_name)
    return raw if isinstance(raw, str) and raw else None


def _first_question(value: object) -> str | None:
    questions = getattr(value, "questions", None)
    if questions is None and isinstance(value, dict):
        questions = value.get("questions")
    if isinstance(questions, (list, tuple)) and questions:
        first = questions[0]
        return first if isinstance(first, str) and first else None
    return None
