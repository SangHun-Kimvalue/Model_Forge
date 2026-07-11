"""Template / parametric recipe system (ADR-0008).

Recipes are versioned parametric objects stored in::

    templates/<recipe_id>/recipe.yaml
    templates/<recipe_id>/model.scad   (or model.py)

Flow:
  1. :class:`RecipeLoader` reads ``recipe.yaml`` and the code source file.
  2. :class:`RecipeRenderer` validates + coerces parameters and renders the
     ``{{param}}`` placeholders in the code template.
  3. :class:`RecipePipeline` drives the rendered code through the standard
     mechanical CAD → validator → slicer chain and records ADR-0005 manifest
     metadata including ``recipe_id``, ``recipe_version``, ``param_hash``,
     and ``code_hash``.
"""

from modules.template.fallback import (
    TemplateFallbackContext,
    TemplateFallbackError,
    TemplateFallbackRender,
    TemplateFallbackRouter,
)
from modules.template.fallback_promotion import promote_fallback_draft
from modules.template.loader import Recipe, RecipeLoader, RecipeLoadError, RecipeNotFoundError
from modules.template.pipeline import RecipePipeline, RecipePipelineArtifacts
from modules.template.renderer import RecipeRenderer, RecipeRenderError, RenderedRecipe
from modules.template.schema import ParameterSpec, RecipeDialect, RecipeManifest, SlicerHints

__all__ = [
    "ParameterSpec",
    "Recipe",
    "RecipeDialect",
    "RecipeLoadError",
    "RecipeLoader",
    "RecipeManifest",
    "RecipeNotFoundError",
    "RecipePipeline",
    "RecipePipelineArtifacts",
    "RecipeRenderError",
    "RecipeRenderer",
    "RenderedRecipe",
    "SlicerHints",
    "TemplateFallbackContext",
    "TemplateFallbackError",
    "TemplateFallbackRender",
    "TemplateFallbackRouter",
    "promote_fallback_draft",
]
