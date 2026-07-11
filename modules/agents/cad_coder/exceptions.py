from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class CADCoderErrorCode(StrEnum):
    """Stable CAD coder failure taxonomy for UI/event/manifest consumers."""

    EMPTY_RESPONSE = "empty_response"
    MISSING_MAIN_CALL = "missing_main_call"
    OPENSCAD_GEOMETRY_ASSIGNMENT = "openscad_geometry_assignment"
    BRACE_UNBALANCED = "brace_unbalanced"
    SYNTAX_CONTRACT_FAILED = "syntax_contract_failed"
    SEMANTIC_FAILURE = "semantic_failure"


class CADCoderAgentError(RuntimeError):
    """Base class for CAD coder agent failures."""

    stage = "cad_coder"

    def __init__(
        self,
        message: str,
        *,
        code: CADCoderErrorCode | str | None = None,
        provider: str | None = None,
        model: str | None = None,
        timeout_s: float | None = None,
        retry_count: int | None = None,
        repair_attempts: int | None = None,
        fallback_recommended: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = CADCoderErrorCode(code) if code is not None else None
        self.provider = provider
        self.model = model
        self.timeout_s = timeout_s
        self.retry_count = retry_count
        self.repair_attempts = repair_attempts
        self.fallback_recommended = fallback_recommended
        merged_metadata: dict[str, Any] = dict(metadata or {})
        if self.code is not None:
            merged_metadata.setdefault("cad_coder_error_code", self.code.value)
        if provider is not None:
            merged_metadata.setdefault("provider", provider)
        if model is not None:
            merged_metadata.setdefault("model", model)
        if timeout_s is not None:
            merged_metadata.setdefault("timeout_s", timeout_s)
        if retry_count is not None:
            merged_metadata.setdefault("retry_count", retry_count)
        if repair_attempts is not None:
            merged_metadata.setdefault("repair_attempts", repair_attempts)
        if fallback_recommended is not None:
            merged_metadata.setdefault("fallback_recommended", fallback_recommended)
        self.metadata = MappingProxyType(merged_metadata)

    @property
    def error_code(self) -> str | None:
        return self.code.value if self.code is not None else None


class CADCoderAgentConfigError(CADCoderAgentError):
    """CAD coder adapter selection or configuration is missing or unsupported."""
