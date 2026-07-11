"""Recipe / parametric template schema (ADR-0008).

Recipes are versioned parametric objects.  File layout::

    templates/<recipe_id>/recipe.yaml       <- parsed into RecipeManifest
    templates/<recipe_id>/model.scad        <- code source (named by code_template)

``RecipeManifest`` is the Pydantic model parsed from ``recipe.yaml``.
The ``RecipeLoader`` resolves ``code_template`` (filename) to code content
and stores it in ``Recipe.code``.

ADR-0008 invariants enforced here:
- Every recipe has an id, version, and deterministic parameter defaults.
- ``id`` must match the directory name (validated by ``RecipeLoader``).
- Parameter names must be unique within a recipe.
- Numeric parameter defaults must satisfy min/max constraints.
"""

from __future__ import annotations

import re
from numbers import Integral, Real
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ParameterType = Literal["float", "int", "str", "bool"]
RecipeDialect = Literal["cadquery", "build123d", "openscad"]


class ParameterSpec(BaseModel):
    """Schema definition for a single recipe parameter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    type: ParameterType
    default: Any
    description: str = ""
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _default_in_range(self) -> ParameterSpec:
        if self.type == "int":
            self._validate_int_default()
        elif self.type == "bool":
            self._validate_bool_default()
        elif self.type == "float":
            try:
                v = float(self.default)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Parameter '{self.name}': default {self.default!r} "
                    f"cannot be converted to {self.type}"
                ) from exc
            if self.min is not None and v < self.min:
                raise ValueError(
                    f"Parameter '{self.name}': default {self.default} < min {self.min}"
                )
            if self.max is not None and v > self.max:
                raise ValueError(
                    f"Parameter '{self.name}': default {self.default} > max {self.max}"
                )
        return self

    def _validate_int_default(self) -> None:
        default = self.default
        if isinstance(default, bool):
            raise ValueError(
                f"Parameter '{self.name}': bool default is not valid for int"
            )
        if isinstance(default, Integral):
            v = float(default)
        elif isinstance(default, Real):
            numeric = float(default)
            if not numeric.is_integer():
                raise ValueError(
                    f"Parameter '{self.name}': int default {default!r} "
                    "must not be fractional"
                )
            v = numeric
        elif isinstance(default, str):
            stripped = default.strip()
            if not re.fullmatch(r"[+-]?\d+", stripped):
                raise ValueError(
                    f"Parameter '{self.name}': int default {default!r} "
                    "must be an integer literal"
                )
            v = float(int(stripped))
        else:
            raise ValueError(
                f"Parameter '{self.name}': default {default!r} "
                "cannot be converted to int"
            )

        if self.min is not None and v < self.min:
            raise ValueError(
                f"Parameter '{self.name}': default {self.default} < min {self.min}"
            )
        if self.max is not None and v > self.max:
            raise ValueError(
                f"Parameter '{self.name}': default {self.default} > max {self.max}"
            )

    def _validate_bool_default(self) -> None:
        default = self.default
        if isinstance(default, bool):
            return
        if isinstance(default, str) and default.strip().lower() in {
            "true",
            "false",
            "1",
            "0",
            "yes",
            "no",
        }:
            return
        raise ValueError(
            f"Parameter '{self.name}': bool default {default!r} "
            "must be an explicit bool literal"
        )


class SlicerHints(BaseModel):
    """Optional slicer parameter suggestions embedded in the recipe."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    layer_height_mm: float | None = Field(default=None, gt=0.0, le=1.0)
    infill_percent: int | None = Field(default=None, ge=0, le=100)


class RecipeManifest(BaseModel):
    """Parsed recipe.yaml structure (ADR-0008).

    ``code_template`` names the code source file relative to the recipe
    directory (e.g. ``model.scad``).  ``RecipeLoader`` resolves it and
    stores the code content in ``Recipe.code``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    version: str = Field(min_length=1)
    title: str = Field(min_length=1)
    category: str = Field(default="general", min_length=1)
    dialect: RecipeDialect
    description: str = ""
    parameters: tuple[ParameterSpec, ...] = ()
    slicer_hints: SlicerHints = Field(default_factory=SlicerHints)
    code_template: str = Field(min_length=1)

    @model_validator(mode="after")
    def _no_duplicate_param_names(self) -> RecipeManifest:
        names = [p.name for p in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate parameter names in recipe")
        return self

    def default_parameters(self) -> dict[str, Any]:
        """Return a mapping of parameter name -> default value."""
        return {p.name: p.default for p in self.parameters}


__all__ = [
    "ParameterSpec",
    "ParameterType",
    "RecipeDialect",
    "RecipeManifest",
    "SlicerHints",
]
