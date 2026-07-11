"""Validation Node — mesh quality gate (DESIGN.md §4.6).

Sits between the generation modules (C / C') and the slicer (D). Reports
manifoldness and wall-thickness issues so the orchestrator can decide
whether to slice, repair, or re-enter the self-healing loop.

Phase 9A adds TrimeshValidatorAdapter for real geometry checks.
"""

from modules.validator.adapters.mock import MockMeshRepairer, MockMeshValidator
from modules.validator.base import BaseMeshRepairer, BaseMeshValidator
from modules.validator.exceptions import (
    RepairError,
    UnsupportedFormatError,
    ValidatorConfigError,
    ValidatorError,
    ValidatorIOError,
)
from modules.validator.factory import ValidatorSettings, create_mesh_validator
from modules.validator.schemas import (
    IssueSeverity,
    MeshFormat,
    RepairRequest,
    RepairResult,
    ValidationCheck,
    ValidationIssue,
    ValidationReport,
    ValidationRequest,
)

__all__ = [
    "BaseMeshRepairer",
    "BaseMeshValidator",
    "IssueSeverity",
    "MeshFormat",
    "MockMeshRepairer",
    "MockMeshValidator",
    "RepairError",
    "RepairRequest",
    "RepairResult",
    "UnsupportedFormatError",
    "ValidationCheck",
    "ValidationIssue",
    "ValidationReport",
    "ValidationRequest",
    "ValidatorConfigError",
    "ValidatorError",
    "ValidatorIOError",
    "ValidatorSettings",
    "create_mesh_validator",
]
