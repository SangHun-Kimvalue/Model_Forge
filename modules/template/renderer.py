"""RecipeRenderer: validate parameters and render {{param}} placeholders.

Template syntax:
    Use ``{{param_name}}`` in the code source file to mark substitution
    points.  The renderer replaces each marker with the validated,
    coerced value for that parameter.

    OpenSCAD's built-in variables (``$fn``, ``$t``, etc.) use ``$`` and
    are therefore unaffected.

Hashes produced (ADR-0008 invariants):
    ``param_hash``
        SHA-256 of the JSON-serialised sorted parameter dict after
        validation and coercion.  Deterministic for the same inputs;
        different for any parameter change.
    ``code_hash``
        SHA-256 of the rendered source code (UTF-8 encoded).  Changes
        whenever parameters or the template source change.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from numbers import Integral, Real
from typing import Any

from modules.template.loader import Recipe
from modules.template.schema import ParameterSpec

_PLACEHOLDER_RE = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")


class RecipeRenderError(ValueError):
    """Raised when parameter validation or template rendering fails."""


@dataclass(frozen=True)
class RenderedRecipe:
    """Output of :class:`RecipeRenderer.render`.

    Attributes
    ----------
    recipe_id:
        Stable identifier from the recipe manifest.
    recipe_version:
        Version string from the recipe manifest.
    code:
        Source code with all ``{{param}}`` placeholders replaced.
    param_hash:
        SHA-256 hex digest of the validated, coerced parameters.
    code_hash:
        SHA-256 hex digest of the rendered source code.
    validated_params:
        Parameters after validation and type coercion.  The keys and
        values are safe to embed in artifact manifest metadata.
    """

    recipe_id: str
    recipe_version: str
    code: str
    param_hash: str
    code_hash: str
    validated_params: dict[str, Any]


class RecipeRenderer:
    """Validate recipe parameters and render source code templates.

    Usage::

        renderer = RecipeRenderer()
        rendered = renderer.render(recipe, {"diameter_mm": 50.0})

    Parameters may be omitted; the recipe's defaults are used.
    Extra parameters (not in the recipe schema) raise :class:`RecipeRenderError`.
    """

    def render(
        self,
        recipe: Recipe,
        parameters: dict[str, Any] | None = None,
    ) -> RenderedRecipe:
        """Validate *parameters* against the recipe schema and render the code.

        Parameters
        ----------
        recipe:
            A fully loaded :class:`~modules.template.loader.Recipe`.
        parameters:
            User-supplied parameter overrides.  Missing keys fall back to
            the recipe defaults.  Extra keys raise :class:`RecipeRenderError`.

        Returns
        -------
        RenderedRecipe
            Rendered source code and computed hashes.

        Raises
        ------
        RecipeRenderError
            On unknown parameters, missing required parameters, type
            coercion failures, out-of-range values, or template
            placeholders that reference undefined parameter names.
        """
        merged: dict[str, Any] = dict(recipe.manifest.default_parameters())
        if parameters:
            # Reject unknown parameters before doing any work.
            known = {p.name for p in recipe.manifest.parameters}
            unknown = set(parameters) - known
            if unknown:
                raise RecipeRenderError(
                    f"Unknown parameters for recipe '{recipe.manifest.id}': "
                    f"{sorted(unknown)}"
                )
            merged.update(parameters)

        # Validate and coerce every declared parameter.
        validated: dict[str, Any] = {}
        for spec in recipe.manifest.parameters:
            if spec.name not in merged:
                raise RecipeRenderError(
                    f"Missing required parameter '{spec.name}' "
                    f"for recipe '{recipe.manifest.id}'"
                )
            validated[spec.name] = self._coerce_and_check(spec, merged[spec.name])

        # Check that every placeholder in the template maps to a known parameter.
        all_refs = set(_PLACEHOLDER_RE.findall(recipe.code))
        known_names = {p.name for p in recipe.manifest.parameters}
        undefined = all_refs - known_names
        if undefined:
            raise RecipeRenderError(
                f"Template for recipe '{recipe.manifest.id}' references "
                f"undefined parameters: {sorted(undefined)}"
            )

        # Compute param_hash before rendering (hash of coerced values).
        param_hash = hashlib.sha256(
            json.dumps(validated, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

        # Substitute all {{param}} markers.
        def _replace(m: re.Match[str]) -> str:
            return str(validated[m.group(1)])

        rendered_code = _PLACEHOLDER_RE.sub(_replace, recipe.code)

        code_hash = hashlib.sha256(rendered_code.encode("utf-8")).hexdigest()

        return RenderedRecipe(
            recipe_id=recipe.manifest.id,
            recipe_version=recipe.manifest.version,
            code=rendered_code,
            param_hash=param_hash,
            code_hash=code_hash,
            validated_params=validated,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_and_check(spec: ParameterSpec, value: object) -> float | int | str | bool:
        """Coerce *value* to the type declared in *spec* and validate range."""
        coerced: float | int | str | bool
        try:
            if spec.type == "float":
                coerced = float(str(value))
            elif spec.type == "int":
                coerced = RecipeRenderer._coerce_int(spec.name, value)
            elif spec.type == "bool":
                coerced = RecipeRenderer._coerce_bool(spec.name, value)
            else:
                coerced = str(value)
        except (ValueError, TypeError) as exc:
            raise RecipeRenderError(
                f"Parameter '{spec.name}': cannot coerce {value!r} to {spec.type}"
            ) from exc

        if spec.type in ("float", "int"):
            fv = float(coerced)
            if spec.min is not None and fv < spec.min:
                raise RecipeRenderError(
                    f"Parameter '{spec.name}': {coerced} < min {spec.min}"
                )
            if spec.max is not None and fv > spec.max:
                raise RecipeRenderError(
                    f"Parameter '{spec.name}': {coerced} > max {spec.max}"
                )

        return coerced

    @staticmethod
    def _coerce_int(name: str, value: object) -> int:
        """Coerce only integral values; never truncate fractional inputs."""
        if isinstance(value, bool):
            raise ValueError("bool is not an int parameter value")
        if isinstance(value, Integral):
            return int(value)
        if isinstance(value, Real):
            numeric = float(value)
            if not numeric.is_integer():
                raise ValueError("fractional value cannot be used as int")
            return int(numeric)
        if isinstance(value, str):
            stripped = value.strip()
            if not re.fullmatch(r"[+-]?\d+", stripped):
                raise ValueError(
                    f"Parameter '{name}': {value!r} is not an integer literal"
                )
            return int(stripped)
        raise ValueError(f"Parameter '{name}': cannot coerce {value!r} to int")

    @staticmethod
    def _coerce_bool(name: str, value: object) -> bool:
        """Coerce explicit bool literals only; reject ambiguous truthiness."""
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            stripped = value.strip().lower()
            if stripped in {"true", "1", "yes"}:
                return True
            if stripped in {"false", "0", "no"}:
                return False
        raise ValueError(f"Parameter '{name}': cannot coerce {value!r} to bool")


__all__ = [
    "RecipeRenderError",
    "RecipeRenderer",
    "RenderedRecipe",
]
