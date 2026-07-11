from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from modules.semantic_validator.adapters.rule_based import RuleBasedSemanticValidator
from modules.semantic_validator.base import BaseSemanticValidator
from modules.semantic_validator.exceptions import SemanticValidatorConfigError

AdapterName = Literal["rule_based"]


class SemanticValidatorSettings(BaseModel):
    """Settings for selecting a semantic validator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    adapter: AdapterName = Field(default="rule_based")
    adapter_label: str | None = None

    @classmethod
    def from_env(
        cls, environ: dict[str, str] | None = None
    ) -> SemanticValidatorSettings:
        source = environ if environ is not None else os.environ
        return cls(
            adapter=source.get("SEMANTIC_VALIDATOR_ADAPTER", "rule_based"),
            adapter_label=source.get("SEMANTIC_VALIDATOR_LABEL"),
        )


def create_semantic_validator(
    settings: SemanticValidatorSettings | None = None,
) -> BaseSemanticValidator:
    selected = settings or SemanticValidatorSettings()
    if selected.adapter == "rule_based":
        return RuleBasedSemanticValidator(adapter_label=selected.adapter_label)
    raise SemanticValidatorConfigError(
        f"Semantic validator adapter '{selected.adapter}' is not recognised."
    )


__all__ = ["SemanticValidatorSettings", "create_semantic_validator"]
