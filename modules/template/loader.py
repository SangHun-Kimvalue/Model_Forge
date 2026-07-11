"""RecipeLoader: scan a catalog directory and load Recipe definitions.

Each recipe lives at::

    <catalog_root>/<recipe_id>/recipe.yaml
    <catalog_root>/<recipe_id>/<code_template>   (e.g. model.scad)

Loader guarantees:
- The YAML ``id`` field must match the directory name.
- The code source file referenced by ``code_template`` must exist.
- ``Recipe.code`` always contains the fully resolved source code, not
  a filename.  Callers never need to re-read the file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from modules.template.schema import RecipeManifest

_RECIPE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class RecipeNotFoundError(LookupError):
    """Raised when the requested recipe_id directory or recipe.yaml is absent."""


class RecipeLoadError(ValueError):
    """Raised when YAML parsing or Pydantic schema validation fails."""


@dataclass(frozen=True)
class Recipe:
    """Fully resolved recipe: manifest metadata + code content.

    ``code`` contains the source file as a string ready to pass to
    :class:`~modules.template.renderer.RecipeRenderer`.
    ``recipe_dir`` is the directory that holds the recipe (useful for
    resolving sibling assets in future phases).
    """

    manifest: RecipeManifest
    code: str
    recipe_dir: Path


class RecipeLoader:
    """Load recipes from a catalog directory.

    Parameters
    ----------
    catalog_root:
        The directory that contains ``<recipe_id>/`` sub-directories.
        Typically ``<project_root>/templates/``.
    """

    def __init__(self, catalog_root: Path) -> None:
        self._root = catalog_root.expanduser().resolve()

    def list(self) -> list[str]:
        """Return a sorted list of available recipe IDs.

        Only directories that contain a ``recipe.yaml`` are included.
        Returns an empty list if the catalog root does not exist.
        """
        if not self._root.is_dir():
            return []
        return sorted(
            p.name
            for p in self._root.iterdir()
            if p.is_dir() and (p / "recipe.yaml").is_file()
        )

    def load(self, recipe_id: str) -> Recipe:
        """Load and validate a recipe by its ID.

        Parameters
        ----------
        recipe_id:
            The name of the sub-directory under ``catalog_root``.

        Raises
        ------
        RecipeNotFoundError
            If the recipe directory or ``recipe.yaml`` does not exist.
        RecipeLoadError
            If YAML parsing or schema validation fails, or if the ``id``
            in ``recipe.yaml`` does not match ``recipe_id``, or if the
            ``code_template`` file is missing.
        """
        if not _RECIPE_ID_RE.fullmatch(recipe_id):
            raise RecipeLoadError(
                f"Invalid recipe_id {recipe_id!r}; expected a single catalog id."
            )

        recipe_dir = (self._root / recipe_id).resolve()
        if recipe_dir.parent != self._root:
            raise RecipeLoadError(
                f"Recipe '{recipe_id}' escapes catalog root {self._root}."
            )
        yaml_path = recipe_dir / "recipe.yaml"
        if not yaml_path.is_file():
            raise RecipeNotFoundError(
                f"Recipe '{recipe_id}' not found at {yaml_path}"
            )

        raw: Any
        try:
            raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise RecipeLoadError(
                f"Failed to parse recipe.yaml for '{recipe_id}': {exc}"
            ) from exc

        if not isinstance(raw, dict):
            raise RecipeLoadError(
                f"recipe.yaml for '{recipe_id}' must be a YAML mapping, "
                f"got {type(raw).__name__}"
            )

        try:
            manifest = RecipeManifest.model_validate(raw)
        except Exception as exc:
            raise RecipeLoadError(
                f"Schema validation failed for recipe '{recipe_id}': {exc}"
            ) from exc

        if manifest.id != recipe_id:
            raise RecipeLoadError(
                f"recipe.yaml id '{manifest.id}' does not match "
                f"directory name '{recipe_id}'"
            )

        raw_code_path = Path(manifest.code_template)
        if raw_code_path.is_absolute():
            raise RecipeLoadError(
                f"code_template '{manifest.code_template}' for recipe '{recipe_id}' "
                "must be relative to the recipe directory."
            )

        code_path = (recipe_dir / raw_code_path).resolve()
        try:
            code_path.relative_to(recipe_dir)
        except ValueError as exc:
            raise RecipeLoadError(
                f"code_template '{manifest.code_template}' for recipe '{recipe_id}' "
                f"escapes recipe directory {recipe_dir}."
            ) from exc
        if not code_path.is_file():
            raise RecipeLoadError(
                f"code_template '{manifest.code_template}' for recipe '{recipe_id}' "
                f"not found at {code_path}"
            )

        code = code_path.read_text(encoding="utf-8")
        return Recipe(manifest=manifest, code=code, recipe_dir=recipe_dir)


__all__ = [
    "Recipe",
    "RecipeLoadError",
    "RecipeLoader",
    "RecipeNotFoundError",
]
