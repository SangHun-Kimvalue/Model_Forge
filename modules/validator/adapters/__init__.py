"""Concrete validator adapters."""

from modules.validator.adapters.mock import (
    MockMeshRepairer,
    MockMeshValidator,
)

__all__ = ["MockMeshRepairer", "MockMeshValidator", "TrimeshValidatorAdapter"]


def __getattr__(name: str) -> object:
    if name == "TrimeshValidatorAdapter":
        from modules.validator.adapters.trimesh import TrimeshValidatorAdapter

        return TrimeshValidatorAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
