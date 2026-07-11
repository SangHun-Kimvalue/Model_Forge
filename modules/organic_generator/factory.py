import logging
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from modules.llm.base import BaseLLMProvider
from modules.llm.factory import LLMProviderSettings, create_llm_provider
from modules.observability.events import mock_selected_event, mock_selected_extra
from modules.organic_generator.adapters.mock import MockOrganicGenerator
from modules.organic_generator.base import BaseOrganicGenerator
from modules.organic_generator.exceptions import OrganicGeneratorConfigError

AdapterName = Literal["mock", "meshy", "triposr", "blender_script"]


class OrganicGeneratorSettings(BaseModel):
    """Runtime settings for selecting an organic generator adapter.

    Preconditions:
        adapter must name the adapter the caller actually intends to use.
        The factory never silently downgrades an unsupported adapter to mock
        (R10 silent-fallback defense).
    Raises:
        OrganicGeneratorConfigError when the selected adapter has no concrete
        implementation in this build.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    adapter: AdapterName = Field(default="mock")
    adapter_label: str | None = Field(default=None)
    meshy_api_key: str | None = Field(default=None)
    meshy_base_url: str | None = Field(default=None)
    meshy_art_style: str = Field(default="realistic")
    blender_binary_path: str | None = Field(default=None)
    blender_expected_version: str | None = Field(default=None)
    blender_timeout_s: float = Field(default=90.0)
    experimental_blender_enabled: bool = Field(default=False)

    @classmethod
    def from_env(
        cls, environ: dict[str, str] | None = None
    ) -> "OrganicGeneratorSettings":
        source = environ if environ is not None else os.environ
        return cls(
            adapter=source.get("ORGANIC_GENERATOR_ADAPTER", "mock"),
            adapter_label=source.get("ORGANIC_GENERATOR_LABEL"),
            meshy_api_key=source.get("MESHY_API_KEY"),
            meshy_base_url=source.get("MESHY_BASE_URL"),
            meshy_art_style=source.get("MESHY_ART_STYLE", "realistic"),
            blender_binary_path=source.get("BLENDER_BINARY_PATH"),
            blender_expected_version=source.get("BLENDER_EXPECTED_VERSION"),
            blender_timeout_s=source.get("ORGANIC_BLENDER_TIMEOUT_S", 90.0),
            experimental_blender_enabled=_env_flag_enabled(
                source.get("MODEL_FORGE_EXPERIMENTAL_BLENDER")
            ),
        )


def create_organic_generator(
    settings: OrganicGeneratorSettings | None = None,
    logger: logging.Logger | None = None,
    llm_provider: BaseLLMProvider | None = None,
) -> BaseOrganicGenerator:
    selected = settings or OrganicGeneratorSettings.from_env()
    log = logger or logging.getLogger(__name__)

    if selected.adapter == "mock":
        label = selected.adapter_label or MockOrganicGenerator.default_adapter_name
        log.warning(
            mock_selected_event("organic_generator"),
            extra=mock_selected_extra(
                component="organic_generator",
                adapter="mock",
                label=label,
            ),
        )
        return MockOrganicGenerator(adapter_label=selected.adapter_label)

    if selected.adapter == "meshy":
        from modules.organic_generator.adapters.meshy import MeshyOrganicGenerator

        if not selected.meshy_api_key:
            raise OrganicGeneratorConfigError(
                "ORGANIC_GENERATOR_ADAPTER=meshy requires MESHY_API_KEY. "
                "Mock fallback is forbidden (DESIGN.md R10)."
            )
        return MeshyOrganicGenerator(
            api_key=selected.meshy_api_key,
            base_url=selected.meshy_base_url or "https://api.meshy.ai",
            adapter_label=selected.adapter_label,
            art_style=selected.meshy_art_style,
        )

    if selected.adapter == "blender_script":
        from modules.organic_generator.adapters.blender_script import (
            BlenderScriptOrganicGenerator,
        )

        if not selected.experimental_blender_enabled:
            raise OrganicGeneratorConfigError(
                "ORGANIC_GENERATOR_ADAPTER=blender_script is an experimental "
                "POC spike and requires MODEL_FORGE_EXPERIMENTAL_BLENDER=1. "
                "It is not part of the official Meshy/TripoSR/Mock organic "
                "adapter roadmap."
            )
        provider = llm_provider
        if provider is None:
            llm_settings = LLMProviderSettings.from_env()
            if llm_settings.provider == "mock":
                raise OrganicGeneratorConfigError(
                    "ORGANIC_GENERATOR_ADAPTER=blender_script requires a real "
                    "LLM provider such as LLM_PROVIDER=gemini. Mock fallback is "
                    "forbidden (DESIGN.md R10)."
                )
            provider = create_llm_provider(llm_settings, logger=log)
        return BlenderScriptOrganicGenerator(
            provider=provider,
            blender_binary_path=selected.blender_binary_path or "blender",
            expected_version=selected.blender_expected_version,
            timeout_s=selected.blender_timeout_s,
            adapter_label=selected.adapter_label,
        )

    raise OrganicGeneratorConfigError(
        f"Organic generator adapter '{selected.adapter}' is selected but no "
        "concrete adapter exists yet. Set ORGANIC_GENERATOR_ADAPTER=mock for "
        "deterministic tests, or implement the adapter first."
    )


def _env_flag_enabled(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}
