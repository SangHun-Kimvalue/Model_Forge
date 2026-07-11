from __future__ import annotations

from abc import ABC, abstractmethod

from modules.semantic_validator.schemas import (
    SemanticValidationReport,
    SemanticValidationRequest,
)


class BaseSemanticValidator(ABC):
    """Contract for checking whether generated CAD preserves user intent."""

    @property
    @abstractmethod
    def adapter_name(self) -> str:
        """Human-readable adapter identifier."""

    @abstractmethod
    def validate(self, request: SemanticValidationRequest) -> SemanticValidationReport:
        """Return a semantic validation report without mutating artifacts."""
