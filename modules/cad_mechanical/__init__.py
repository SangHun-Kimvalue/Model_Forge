"""Module C — mechanical CAD generation (DESIGN.md §4.3).

LLM-authored DSL (CadQuery / Build123d / OpenSCAD) is rendered to a 3D
mesh under a sandbox boundary. Phase 4 ships the contract, mock adapter,
and Docker-backed boundary; Phase 9A adds the SubprocessSandboxRunner
(local-dev-only) and CadQueryAdapter backed by a real sandbox runner.
"""

from modules.cad_mechanical.base import BaseMechanicalCADGenerator
from modules.cad_mechanical.exceptions import (
    CadMechanicalConfigError,
    CadMechanicalError,
    GenerationError,
    SandboxError,
    SandboxMemoryError,
    SandboxTimeoutError,
    TopologyError,
)
from modules.cad_mechanical.factory import (
    CadMechanicalSettings,
    create_mechanical_cad_generator,
)
from modules.cad_mechanical.runner import (
    SANDBOX_ARTIFACT_STEM,
    DockerSandboxRunner,
    RejectAllRunner,
    SandboxRunner,
    SubprocessSandboxRunner,
)
from modules.cad_mechanical.schemas import (
    CADDialect,
    GenerationRequest,
    GenerationResult,
    MeshFormat,
    SandboxExecutionResult,
    SandboxLimits,
)

__all__ = [
    "BaseMechanicalCADGenerator",
    "CADDialect",
    "CadMechanicalConfigError",
    "CadMechanicalError",
    "CadMechanicalSettings",
    "DockerSandboxRunner",
    "GenerationError",
    "GenerationRequest",
    "GenerationResult",
    "MeshFormat",
    "OpenSCADAdapter",
    "OpenSCADDockerRunner",
    "RejectAllRunner",
    "SANDBOX_ARTIFACT_STEM",
    "SandboxError",
    "SandboxExecutionResult",
    "SandboxLimits",
    "SandboxMemoryError",
    "SandboxRunner",
    "SandboxTimeoutError",
    "SubprocessSandboxRunner",
    "TopologyError",
    "create_mechanical_cad_generator",
]


def __getattr__(name: str) -> object:
    if name == "OpenSCADAdapter":
        from modules.cad_mechanical.adapters.openscad import OpenSCADAdapter

        return OpenSCADAdapter
    if name == "OpenSCADDockerRunner":
        from modules.cad_mechanical.adapters.openscad import OpenSCADDockerRunner

        return OpenSCADDockerRunner
    raise AttributeError(name)
