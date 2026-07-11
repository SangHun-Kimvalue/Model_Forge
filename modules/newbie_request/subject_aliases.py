from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

AliasRouteReason = Literal[
    "exact_decorative_keyring_subject",
    "draftable_decorative_keyring_subject",
    "missing_mechanical_dimensions",
    "unsupported_or_ambiguous_route",
    "ambiguous_decorative_subject",
]

DEFAULT_SUBJECT_ALIAS_TABLE_PATH = (
    Path(__file__).resolve().parent / "data" / "subject_alias_table.json"
)
DEFAULT_TRADEMARK_BLOCKED_SUBJECTS_PATH = (
    Path(__file__).resolve().parent / "data" / "trademark_blocked_subjects.json"
)


class SubjectAliasError(ValueError):
    """Raised when deterministic subject alias fixtures are invalid."""


class SubjectAliasRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    alias_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_]*$")
    terms: tuple[str, ...] = Field(min_length=1)
    category: str = Field(min_length=1)
    object_type: str | None = None
    subject: str | None = None
    style: str | None = None
    request_id: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9_]*$",
    )
    required_features: tuple[str, ...] = ()
    route_reason: AliasRouteReason
    questions: tuple[str, ...] = ()
    exclude_terms: tuple[str, ...] = ()
    confidence: float = Field(default=0.9, ge=0, le=1)

    @model_validator(mode="after")
    def _has_route_payload(self) -> SubjectAliasRow:
        if not self.category and self.subject is None and self.style is None:
            raise ValueError("Alias row must define category, subject, or style")
        return self


class SubjectAliasTable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(min_length=1)
    aliases: tuple[SubjectAliasRow, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _terms_are_unique(self) -> SubjectAliasTable:
        seen: dict[str, str] = {}
        for row in self.aliases:
            for term in (*row.terms, *row.exclude_terms):
                normalized = normalize_alias_term(term)
                if not normalized:
                    raise ValueError(f"Alias row {row.alias_id} contains empty term")
                previous = seen.get(normalized)
                if previous is not None and previous != row.alias_id:
                    raise ValueError(
                        "Duplicate normalized alias term "
                        f"{normalized!r} in {previous!r} and {row.alias_id!r}"
                    )
                seen[normalized] = row.alias_id
        return self

    def matches(self, prompt: str) -> tuple[SubjectAliasRow, ...]:
        matches: list[SubjectAliasRow] = []
        for row in self.aliases:
            if any(_term_matches(prompt, term) for term in row.exclude_terms):
                continue
            if any(_term_matches(prompt, term) for term in row.terms):
                matches.append(row)
        return tuple(matches)


class TrademarkBlockedSubject(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    blocked_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_]*$")
    terms: tuple[str, ...] = Field(min_length=1)
    category: str = Field(min_length=1)
    subject: str | None = None
    style: str | None = None
    reason: str = Field(min_length=1)
    questions: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _has_route_payload(self) -> TrademarkBlockedSubject:
        if not self.category and self.subject is None and self.style is None:
            raise ValueError(
                "Trademark row must define category, subject, or style"
            )
        return self


class TrademarkBlockedSubjectTable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(min_length=1)
    blocked_subjects: tuple[TrademarkBlockedSubject, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _terms_are_unique(self) -> TrademarkBlockedSubjectTable:
        seen: dict[str, str] = {}
        for row in self.blocked_subjects:
            for term in row.terms:
                normalized = normalize_alias_term(term)
                if not normalized:
                    raise ValueError(f"Trademark row {row.blocked_id} has empty term")
                previous = seen.get(normalized)
                if previous is not None and previous != row.blocked_id:
                    raise ValueError(
                        "Duplicate normalized trademark term "
                        f"{normalized!r} in {previous!r} and {row.blocked_id!r}"
                    )
                seen[normalized] = row.blocked_id
        return self

    def match(self, prompt: str) -> TrademarkBlockedSubject | None:
        for row in self.blocked_subjects:
            if any(_term_matches(prompt, term) for term in row.terms):
                return row
        return None


def normalize_alias_term(value: str) -> str:
    return "".join(
        char
        for char in value.casefold()
        if not char.isspace() and char not in {"-", "_"}
    )


def _term_matches(prompt: str, term: str) -> bool:
    normalized_term = normalize_alias_term(term)
    if not normalized_term:
        return False
    if _requires_token_exact(term, normalized_term):
        return normalized_term in _normalized_tokens(prompt)
    return normalized_term in normalize_alias_term(prompt)


def _requires_token_exact(term: str, normalized_term: str) -> bool:
    if all(char.isascii() and char.isalnum() for char in normalized_term):
        return not any(separator in term for separator in (" ", "\t", "\n", "-", "_"))
    return len(normalized_term) == 1


def _normalized_tokens(value: str) -> set[str]:
    tokens: set[str] = set()
    current: list[str] = []
    for char in value:
        if char.isalnum():
            current.append(char)
            continue
        if current:
            tokens.add(normalize_alias_term("".join(current)))
            current = []
    if current:
        tokens.add(normalize_alias_term("".join(current)))
    return {token for token in tokens if token}


def load_subject_alias_table(
    path: Path = DEFAULT_SUBJECT_ALIAS_TABLE_PATH,
) -> SubjectAliasTable:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return SubjectAliasTable.model_validate(payload)
    except Exception as exc:
        raise SubjectAliasError(f"Invalid subject alias table: {path}") from exc


def load_trademark_blocked_subjects(
    path: Path = DEFAULT_TRADEMARK_BLOCKED_SUBJECTS_PATH,
) -> TrademarkBlockedSubjectTable:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return TrademarkBlockedSubjectTable.model_validate(payload)
    except Exception as exc:
        raise SubjectAliasError(f"Invalid trademark subject table: {path}") from exc
