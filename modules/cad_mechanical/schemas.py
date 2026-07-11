from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MeshFormat = Literal["stl", "step"]


class CADDialect(StrEnum):
    """DSL dialect the LLM-authored script targets.

    Each dialect is implemented by exactly one concrete adapter (Strategy,
    DESIGN.md §4.3). The mock adapter accepts all dialects and emits a
    deterministic stub artifact so downstream stages can still operate.
    """

    CADQUERY = "cadquery"
    BUILD123D = "build123d"
    OPENSCAD = "openscad"


class SandboxLimits(BaseModel):
    """Per-call sandbox quotas applied by ``SandboxRunner`` implementations.

    Defaults reflect the policy fixed in `docs/decisions/ADR-0001-cad-sandbox.md`
    (R4 RCE defense): 30 s wall clock, 512 MiB resident memory, 1 logical
    CPU, no network. Concrete runners may tighten further but must never
    silently relax these limits.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    timeout_s: float = Field(default=30.0, gt=0.0, le=600.0)
    max_memory_mb: int = Field(default=512, gt=0, le=8192)
    max_cpu_cores: float = Field(default=1.0, gt=0.0, le=8.0)
    network_enabled: bool = Field(default=False)


class GenerationRequest(BaseModel):
    """Provider-neutral mechanical CAD generation request.

    Preconditions:
        - ``code`` is an LLM-authored DSL script targeting ``dialect``.
        - ``session_id`` is a stable identifier so adapters can co-locate
          intermediate artifacts inside ``output_dir``.
        - ``output_dir`` exists and is writable by the process.
    Raises:
        pydantic.ValidationError when fields are missing or out of range.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str = Field(min_length=1)
    code: str = Field(min_length=1)
    dialect: CADDialect
    session_id: str = Field(min_length=1)
    output_dir: Path
    format: MeshFormat = "stl"
    seed: int | None = None
    limits: SandboxLimits = Field(default_factory=SandboxLimits)


class GenerationResult(BaseModel):
    """Provider-neutral mechanical CAD generation result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    format: MeshFormat
    dialect: CADDialect
    adapter_used: str
    generation_time_s: float = Field(ge=0.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SandboxExecutionResult(BaseModel):
    """Outcome of a single ``SandboxRunner.execute`` call.

    The runner contract is intentionally minimal: it reports the artifact
    the script produced, what it printed, and how long / how much memory it
    used. Translating this into ``GenerationResult`` is the adapter's job.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_path: Path
    stdout: str = ""
    stderr: str = ""
    duration_s: float = Field(ge=0.0)
    peak_memory_mb: float = Field(ge=0.0, default=0.0)
