from __future__ import annotations

from abc import ABC, abstractmethod

from modules.requirements.schemas import RequirementExtractionRequest, RequirementSpec


class BaseRequirementExtractor(ABC):
    """Contract for turning user natural language into traceable CAD intent."""

    @property
    @abstractmethod
    def adapter_name(self) -> str:
        """Human-readable adapter identifier."""

    @abstractmethod
    def extract(self, request: RequirementExtractionRequest) -> RequirementSpec:
        """Return a conservative requirement spec for the user prompt."""
