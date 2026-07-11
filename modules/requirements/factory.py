from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from modules.requirements.adapters.rule_based import RuleBasedRequirementExtractor
from modules.requirements.base import BaseRequirementExtractor
from modules.requirements.exceptions import RequirementConfigError

AdapterName = Literal["rule_based"]


class RequirementExtractorSettings(BaseModel):
    """Settings for selecting a requirement extractor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    adapter: AdapterName = Field(default="rule_based")
    adapter_label: str | None = None

    @classmethod
    def from_env(
        cls, environ: dict[str, str] | None = None
    ) -> RequirementExtractorSettings:
        source = environ if environ is not None else os.environ
        return cls(
            adapter=source.get("REQUIREMENT_EXTRACTOR_ADAPTER", "rule_based"),
            adapter_label=source.get("REQUIREMENT_EXTRACTOR_LABEL"),
        )


def create_requirement_extractor(
    settings: RequirementExtractorSettings | None = None,
) -> BaseRequirementExtractor:
    selected = settings or RequirementExtractorSettings()
    if selected.adapter == "rule_based":
        return RuleBasedRequirementExtractor(adapter_label=selected.adapter_label)
    raise RequirementConfigError(
        f"Requirement extractor adapter '{selected.adapter}' is not recognised."
    )


__all__ = ["RequirementExtractorSettings", "create_requirement_extractor"]
