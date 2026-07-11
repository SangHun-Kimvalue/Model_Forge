import logging
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from modules.observability.events import mock_selected_event, mock_selected_extra
from modules.validator.adapters.mock import MockMeshValidator
from modules.validator.base import BaseMeshValidator
from modules.validator.exceptions import ValidatorConfigError

AdapterName = Literal["mock", "trimesh"]


class ValidatorSettings(BaseModel):
    """Settings for the mesh validator.

    ``adapter="mock"`` — pure-Python ASCII STL/OBJ parser; no extra deps.
    ``adapter="trimesh"`` — real geometry checks via the trimesh library;
        requires ``pip install 'model_forge[trimesh]'``.

    The factory never silently downgrades an unsupported adapter to mock
    (DESIGN.md R10 — no silent fallback).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    adapter: AdapterName = Field(default="mock")
    adapter_label: str | None = Field(default=None)

    @classmethod
    def from_env(
        cls, environ: dict[str, str] | None = None
    ) -> "ValidatorSettings":
        source = environ if environ is not None else os.environ
        return cls(
            adapter=source.get("VALIDATOR_ADAPTER", "mock"),
            adapter_label=source.get("VALIDATOR_LABEL"),
        )


def create_mesh_validator(
    settings: ValidatorSettings | None = None,
    logger: logging.Logger | None = None,
) -> BaseMeshValidator:
    selected = settings or ValidatorSettings.from_env()
    log = logger or logging.getLogger(__name__)

    if selected.adapter == "mock":
        label = selected.adapter_label or MockMeshValidator.default_adapter_name
        log.warning(
            mock_selected_event("validator"),
            extra=mock_selected_extra(
                component="validator",
                adapter="mock",
                label=label,
            ),
        )
        return MockMeshValidator(adapter_label=selected.adapter_label)

    if selected.adapter == "trimesh":
        from modules.validator.adapters.trimesh import TrimeshValidatorAdapter

        adapter = TrimeshValidatorAdapter(adapter_label=selected.adapter_label)
        if not adapter.health_check():
            raise ValidatorConfigError(
                "VALIDATOR_ADAPTER=trimesh but trimesh is not installed. "
                "Install it with: pip install 'model_forge[trimesh]'"
            )
        return adapter

    raise ValidatorConfigError(
        f"Validator adapter '{selected.adapter}' is not recognised. "
        "Valid values: mock, trimesh."
    )


__all__ = ["ValidatorSettings", "create_mesh_validator"]
