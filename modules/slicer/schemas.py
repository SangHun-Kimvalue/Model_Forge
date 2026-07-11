from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MeshInputFormat = Literal["stl", "obj", "3mf"]


class PrinterProfile(BaseModel):
    """Pinned printer profile (DESIGN.md §4.5, R8).

    The orchestrator does not generate profiles on the fly — they are
    selected from a curated set so a slicing job is fully reproducible
    given ``(printer, material, layer_height, infill, seed)``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    nozzle_diameter_mm: float = Field(gt=0.0, le=2.0)
    bed_size_mm: tuple[float, float, float]
    firmware_flavor: Literal["marlin", "klipper", "rrf"] = "marlin"

    @model_validator(mode="after")
    def _bed_size_must_be_positive(self) -> "PrinterProfile":
        if any(axis <= 0 for axis in self.bed_size_mm):
            raise ValueError("PrinterProfile.bed_size_mm axes must all be > 0")
        return self


class MaterialPreset(BaseModel):
    """Pinned material preset (filament profile)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    nozzle_temp_c: int = Field(gt=0, le=400)
    bed_temp_c: int = Field(ge=0, le=200)
    flow_ratio: float = Field(default=1.0, gt=0.0, le=2.0)


class SliceRequest(BaseModel):
    """Provider-neutral slicing request.

    Preconditions:
        ``mesh_path`` exists in ``mesh_format``. ``output_dir`` exists
        and is writable. ``printer`` / ``material`` are pinned profiles
        — slicers do not look these up themselves.
    Raises:
        pydantic.ValidationError when fields are missing or out of range.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mesh_path: Path
    mesh_format: MeshInputFormat
    session_id: str = Field(min_length=1)
    output_dir: Path
    printer: PrinterProfile
    material: MaterialPreset
    process_profile: str | None = Field(default=None, min_length=1)
    layer_height_mm: float = Field(default=0.2, gt=0.0, le=1.0)
    infill_percent: int = Field(default=20, ge=0, le=100)
    seed: int | None = None


class SliceResult(BaseModel):
    """Provider-neutral slicing result.

    ``threemf_path`` is optional because not every adapter emits a 3MF
    snapshot; the G-code path is the only mandatory artifact. Estimates
    are reported by the slicer itself — the harness does not recompute
    them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    gcode_path: Path
    threemf_path: Path | None = None
    adapter_used: str = Field(min_length=1)
    slicer_version: str = Field(min_length=1)
    layer_count: int = Field(ge=0)
    estimated_print_time_s: float = Field(ge=0.0)
    estimated_filament_g: float = Field(ge=0.0)
    slicing_time_s: float = Field(ge=0.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "MaterialPreset",
    "MeshInputFormat",
    "PrinterProfile",
    "SliceRequest",
    "SliceResult",
]
