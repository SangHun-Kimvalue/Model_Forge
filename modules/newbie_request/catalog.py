from __future__ import annotations

import json
from pathlib import Path

from modules.newbie_request.schemas import ComponentCatalog, NewbieRequestCatalog

DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_CATALOG_PATH = DATA_DIR / "newbie_request_catalog.json"
DEFAULT_COMPONENT_CATALOG_PATH = DATA_DIR / "component_catalog.json"


def load_newbie_request_catalog(
    path: str | Path | None = None,
) -> NewbieRequestCatalog:
    """Load the Phase 12A beginner request catalog."""

    catalog_path = Path(path) if path is not None else DEFAULT_CATALOG_PATH
    with catalog_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return NewbieRequestCatalog.model_validate(payload)


def load_component_catalog(
    path: str | Path | None = None,
) -> ComponentCatalog:
    """Load the package component capability catalog."""

    catalog_path = Path(path) if path is not None else DEFAULT_COMPONENT_CATALOG_PATH
    with catalog_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return ComponentCatalog.model_validate(payload)


__all__ = [
    "DEFAULT_CATALOG_PATH",
    "DEFAULT_COMPONENT_CATALOG_PATH",
    "load_component_catalog",
    "load_newbie_request_catalog",
]
