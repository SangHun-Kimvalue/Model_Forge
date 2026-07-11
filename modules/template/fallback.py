"""Explicit recipe fallback routing for failed freeform CAD attempts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from modules.requirements.schemas import RequirementSpec
from modules.template.loader import (
    RecipeLoader,
    RecipeLoadError,
    RecipeNotFoundError,
)
from modules.template.renderer import RecipeRenderer, RecipeRenderError

__all__ = [
    "TemplateFallbackContext",
    "TemplateFallbackError",
    "TemplateFallbackRender",
    "TemplateFallbackRouter",
]


@dataclass(frozen=True)
class TemplateFallbackContext:
    """Signals used to choose a deterministic recipe fallback."""

    description: str
    requirements: RequirementSpec | None
    suggested_template_id: str | None = None
    fallback_reason: str | None = None
    provider: str | None = None
    model: str | None = None
    repair_attempts: int | None = None


@dataclass(frozen=True)
class TemplateFallbackRender:
    """Rendered recipe code and provenance for a fallback attempt."""

    recipe_id: str
    recipe_version: str
    code: str
    parameters: dict[str, Any]
    param_hash: str
    code_hash: str


class TemplateFallbackError(RuntimeError):
    """Raised when a matched fallback recipe cannot be loaded or rendered."""

    stage = "template_fallback"

    def __init__(
        self,
        message: str,
        *,
        template_id: str,
        code: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.template_id = template_id
        self.template_fallback_error_code = code
        self.template_fallback_error_metadata = dict(metadata or {})


class TemplateFallbackRouter:
    """Map simple known object types to deterministic recipe templates."""

    def __init__(self, *, loader: RecipeLoader, renderer: RecipeRenderer) -> None:
        self._loader = loader
        self._renderer = renderer

    def match_template_id(self, context: TemplateFallbackContext) -> str | None:
        """Return a catalog recipe id for the context, if one is safe."""
        inferred = self._inferred_template_id(context)
        if context.suggested_template_id:
            return (
                context.suggested_template_id
                if context.suggested_template_id == inferred
                else None
            )
        return inferred

    def _inferred_template_id(self, context: TemplateFallbackContext) -> str | None:
        """Return the conservative recipe match without honoring suggestions."""

        object_type = (
            context.requirements.object_type if context.requirements else None
        )
        description = context.description.lower()
        if object_type == "cup" or any(
            marker in description
            for marker in ("cup", "컵", "container", "open container")
        ):
            return "simple-cup"
        if object_type == "bracket" or "bracket" in description or "브라켓" in description:
            return "simple-bracket"

        # High-complexity objects such as drone frames intentionally do not
        # fallback to a generic recipe in L3E. They need a curated recipe.
        return None

    def render(
        self,
        context: TemplateFallbackContext,
    ) -> TemplateFallbackRender | None:
        """Render the matched recipe, or return ``None`` when no safe match exists."""
        recipe_id = self.match_template_id(context)
        if recipe_id is None:
            return None
        try:
            recipe = self._loader.load(recipe_id)
            rendered = self._renderer.render(
                recipe,
                _template_parameter_overrides(recipe_id, context),
            )
        except RecipeNotFoundError as exc:
            raise _template_fallback_error(
                recipe_id,
                context,
                code="template_not_found",
                cause=exc,
            ) from exc
        except RecipeLoadError as exc:
            raise _template_fallback_error(
                recipe_id,
                context,
                code="template_load_failed",
                cause=exc,
            ) from exc
        except RecipeRenderError as exc:
            raise _template_fallback_error(
                recipe_id,
                context,
                code="template_render_failed",
                cause=exc,
            ) from exc
        return TemplateFallbackRender(
            recipe_id=rendered.recipe_id,
            recipe_version=rendered.recipe_version,
            code=rendered.code,
            parameters=rendered.validated_params,
            param_hash=rendered.param_hash,
            code_hash=rendered.code_hash,
        )


def _template_parameter_overrides(
    recipe_id: str,
    context: TemplateFallbackContext,
) -> dict[str, object]:
    description = context.description
    if recipe_id == "simple-cup":
        return _cup_parameter_overrides(description)
    if recipe_id == "simple-bracket":
        return _bracket_parameter_overrides(description)
    return {}


def _cup_parameter_overrides(description: str) -> dict[str, object]:
    params: dict[str, object] = {}
    diameter = _find_named_mm(
        description,
        ("outer diameter", "diameter", "지름", "외경"),
    )
    height = _find_named_mm(description, ("height", "높이"))
    wall = _find_named_mm(description, ("wall thickness", "wall", "벽 두께", "벽", "두께"))
    if diameter is not None:
        params["outer_diameter_mm"] = diameter
    if height is not None:
        params["height_mm"] = height
    if wall is not None:
        params["wall_mm"] = wall
    return params


def _bracket_parameter_overrides(description: str) -> dict[str, object]:
    params: dict[str, object] = {}
    dimensions = _find_dimension_triplet(description) or _find_dimension_pair(description)
    if dimensions is not None:
        params["length_mm"] = dimensions[0]
        params["width_mm"] = dimensions[1]
        if len(dimensions) >= 3:
            params["thickness_mm"] = dimensions[2]

    thickness = _find_named_mm(description, ("thickness", "두께"))
    if thickness is not None:
        params["thickness_mm"] = thickness

    hole_diameter = _find_hole_diameter_mm(description)
    if hole_diameter is not None:
        params["hole_diameter_mm"] = hole_diameter

    hole_count = _find_hole_count(description)
    if hole_count is not None:
        params["hole_count"] = hole_count
    return params


def _find_dimension_triplet(description: str) -> tuple[float, float, float] | None:
    match = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:x|×|by|,)\s*"
        r"(\d+(?:\.\d+)?)\s*(?:x|×|by|,)\s*"
        r"(\d+(?:\.\d+)?)\s*(?:mm|밀리)",
        description,
        re.IGNORECASE,
    )
    if not match:
        return None
    return (float(match.group(1)), float(match.group(2)), float(match.group(3)))


def _find_dimension_pair(description: str) -> tuple[float, float] | None:
    match = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:x|×|by|,)\s*"
        r"(\d+(?:\.\d+)?)\s*(?:mm|밀리)",
        description,
        re.IGNORECASE,
    )
    if not match:
        return None
    return (float(match.group(1)), float(match.group(2)))


def _find_named_mm(description: str, names: tuple[str, ...]) -> float | None:
    joined = "|".join(re.escape(name) for name in names)
    patterns = (
        rf"(?:{joined})\s*(?:은|는|:|=|약)?\s*(\d+(?:\.\d+)?)\s*(?:mm|밀리)?",
        rf"(\d+(?:\.\d+)?)\s*(?:mm|밀리)\s*(?:의\s*)?(?:{joined})",
    )
    for pattern in patterns:
        match = re.search(pattern, description, re.IGNORECASE)
        if match:
            return float(match.group(1))
    return None


def _find_hole_diameter_mm(description: str) -> float | None:
    patterns = (
        r"(?:구멍|홀|hole)[^.\n,;]{0,20}(?:지름|직경|diameter|d)"
        r"\s*(?:은|는|:|=|약)?\s*(\d+(?:\.\d+)?)\s*(?:mm|밀리)?",
        r"(?:지름|직경|diameter|d)\s*(?:은|는|:|=|약)?\s*"
        r"(\d+(?:\.\d+)?)\s*(?:mm|밀리)?[^.\n,;]{0,20}(?:구멍|홀|hole)",
    )
    for pattern in patterns:
        match = re.search(pattern, description, re.IGNORECASE)
        if match:
            return float(match.group(1))
    return None


def _find_hole_count(description: str) -> int | None:
    patterns = (
        r"(\d+)\s*개\s*(?:의\s*)?(?:나사\s*)?(?:구멍|홀)",
        r"(?:구멍|홀)\s*(\d+)\s*개",
        r"(\d+)\s*(?:mounting\s*)?holes?",
    )
    for pattern in patterns:
        match = re.search(pattern, description, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _template_fallback_error(
    recipe_id: str,
    context: TemplateFallbackContext,
    *,
    code: str,
    cause: Exception,
) -> TemplateFallbackError:
    return TemplateFallbackError(
        f"Template fallback '{recipe_id}' failed: {cause}",
        template_id=recipe_id,
        code=code,
        metadata={
            "template_id": recipe_id,
            "fallback_reason": context.fallback_reason,
            "provider": context.provider,
            "model": context.model,
            "repair_attempts": context.repair_attempts,
            "cause_type": type(cause).__name__,
            "cause_detail": str(cause),
        },
    )
