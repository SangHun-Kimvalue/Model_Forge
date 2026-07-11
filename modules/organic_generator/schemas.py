from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MeshFormat = Literal["stl", "obj"]


class Quality(StrEnum):
    """Generation quality tier exposed to upstream agents."""

    DRAFT = "draft"
    STANDARD = "standard"
    HIGH = "high"


class GenerationRequest(BaseModel):
    """Provider-neutral text-to-3D request.

    Preconditions:
        prompt must be non-empty; session_id must be a stable identifier so
        adapters can co-locate intermediate artifacts; output_dir must point
        to a directory the adapter is allowed to write into.
    Raises:
        pydantic.ValidationError if required fields are absent or malformed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    output_dir: Path
    format: MeshFormat = "stl"
    quality: Quality = Quality.STANDARD
    seed: int | None = None
    negative_prompt: str | None = None


class GenerationResult(BaseModel):
    """Provider-neutral text-to-3D result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    format: MeshFormat
    adapter_used: str
    generation_time_s: float = Field(ge=0.0)
    metadata: dict[str, Any] = Field(default_factory=dict)
