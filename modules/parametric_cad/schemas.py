"""Validated manufacturing parameters for template-driven CAD generation."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PartType = Literal[
    "drone_frame",
    "cup",
    "bracket",
    "pen_holder",
    "fixture",
]
LogoMode = Literal["none", "embossed", "debossed"]


class LogoSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(default="")
    mode: LogoMode = Field(default="none")
    placement: str = Field(default="top_surface")
    height_mm: float = Field(default=1.0, ge=0.2, le=5.0)

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()[:32]

    @field_validator("height_mm", mode="before")
    @classmethod
    def default_height(cls, value: Any) -> Any:
        return 1.0 if value is None else value

    @model_validator(mode="after")
    def clear_text_when_disabled(self) -> LogoSpec:
        if self.mode == "none":
            object.__setattr__(self, "text", "")
        return self


class ManufacturingParams(BaseModel):
    """Small, template-safe contract between an LLM and CAD rendering code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    part_type: PartType
    outer_size_mm: tuple[float, float] = Field(default=(80.0, 80.0))
    height_mm: float = Field(default=20.0, ge=1.0, le=310.0)
    thickness_mm: float = Field(default=4.0, ge=1.0, le=20.0)
    wall_thickness_mm: float = Field(default=2.0, ge=0.8, le=10.0)
    exact_outer_size: bool = Field(default=False)
    hole_count: int = Field(default=0, ge=0, le=16)
    hole_diameter_mm: float = Field(default=4.0, ge=1.0, le=40.0)
    motor_mount_count: int = Field(default=4, ge=0, le=8)
    motor_mount_diameter_mm: float = Field(default=24.0, ge=6.0, le=60.0)
    logo: LogoSpec = Field(default_factory=LogoSpec)
    notes: str = Field(default="")

    @field_validator("outer_size_mm")
    @classmethod
    def validate_outer_size(cls, value: tuple[float, float]) -> tuple[float, float]:
        if len(value) != 2:
            raise ValueError("outer_size_mm must contain X and Y dimensions.")
        x, y = value
        if x < 5.0 or y < 5.0 or x > 310.0 or y > 310.0:
            raise ValueError("outer_size_mm must stay within 5..310 mm.")
        return (float(x), float(y))

    @field_validator(
        "height_mm",
        "thickness_mm",
        "wall_thickness_mm",
        "hole_diameter_mm",
        "motor_mount_diameter_mm",
        mode="before",
    )
    @classmethod
    def default_numeric_fields(cls, value: Any, info: Any) -> Any:
        if isinstance(value, list) and value:
            value = value[0]
        if value is not None:
            try:
                if float(value) > 0:
                    return value
            except (TypeError, ValueError):
                return value
        field_name = getattr(info, "field_name", "")
        defaults: dict[str, float] = {
            "height_mm": 20.0,
            "thickness_mm": 4.0,
            "wall_thickness_mm": 2.0,
            "hole_diameter_mm": 4.0,
            "motor_mount_diameter_mm": 24.0,
        }
        return defaults[field_name]

    @field_validator("hole_count", "motor_mount_count", mode="before")
    @classmethod
    def default_count_fields(cls, value: Any, info: Any) -> Any:
        if value is not None:
            return value
        field_name = getattr(info, "field_name", "")
        return 4 if field_name == "motor_mount_count" else 0

    @field_validator("notes")
    @classmethod
    def trim_notes(cls, value: str) -> str:
        return value.strip()[:240]

    @model_validator(mode="after")
    def validate_part_constraints(self) -> ManufacturingParams:
        x, y = self.outer_size_mm
        if self.part_type == "cup":
            min_wall_span = max(self.wall_thickness_mm * 2.5, 8.0)
            if min(x, y) <= min_wall_span:
                raise ValueError("cup outer_size_mm is too small for wall thickness.")
        if self.part_type == "drone_frame":
            if self.motor_mount_count not in {4}:
                raise ValueError("drone_frame template currently supports 4 motor mounts.")
            if self.motor_mount_diameter_mm >= min(x, y) / 2:
                raise ValueError("motor mounts are too large for the requested frame.")
        if self.part_type in {"fixture", "bracket"} and self.hole_count == 0:
            object.__setattr__(self, "hole_count", 4 if self.part_type == "fixture" else 2)
        if self.part_type == "pen_holder" and self.hole_count == 0:
            object.__setattr__(self, "hole_count", 3)
        return self
