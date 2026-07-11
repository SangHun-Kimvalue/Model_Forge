import logging
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from modules.agents.cad_coder.adapters.mock import MockCADCoderAgent
from modules.agents.cad_coder.adapters.prompt_based import PromptBasedCADCoderAgent
from modules.agents.cad_coder.base import BaseCADCoderAgent
from modules.agents.cad_coder.exceptions import CADCoderAgentConfigError
from modules.llm.base import BaseLLMProvider
from modules.llm.factory import LLMProviderSettings, create_llm_provider
from modules.observability.events import mock_selected_event, mock_selected_extra

CADCoderAdapterName = Literal["mock", "prompt_based"]


class CADCoderAgentSettings(BaseModel):
    """Runtime settings for selecting a CAD coder adapter.

    Preconditions:
        adapter must name the adapter the caller actually intends to use.
        The factory never silently downgrades to mock (R10 silent-fallback
        defense).
    Raises:
        CADCoderAgentConfigError when the selected adapter has no concrete
        implementation in this build.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    adapter: CADCoderAdapterName = Field(default="mock")
    adapter_label: str | None = Field(default=None)

    @classmethod
    def from_env(
        cls, environ: dict[str, str] | None = None
    ) -> "CADCoderAgentSettings":
        source = environ if environ is not None else os.environ
        return cls(
            adapter=source.get("CAD_CODER_AGENT_ADAPTER", "mock"),
            adapter_label=source.get("CAD_CODER_AGENT_LABEL"),
        )


def create_cad_coder_agent(
    settings: CADCoderAgentSettings | None = None,
    logger: logging.Logger | None = None,
    *,
    llm_provider: BaseLLMProvider | None = None,
) -> BaseCADCoderAgent:
    selected = settings or CADCoderAgentSettings.from_env()
    log = logger or logging.getLogger(__name__)

    if selected.adapter == "mock":
        label = selected.adapter_label or MockCADCoderAgent.default_adapter_name
        log.warning(
            mock_selected_event("cad_coder_agent"),
            extra=mock_selected_extra(
                component="cad_coder_agent",
                adapter="mock",
                label=label,
            ),
        )
        return MockCADCoderAgent(adapter_label=selected.adapter_label)

    if selected.adapter == "prompt_based":
        if llm_provider is None:
            llm_settings = LLMProviderSettings.from_env()
            if llm_settings.provider == "mock":
                raise CADCoderAgentConfigError(
                    "CAD_CODER_AGENT_ADAPTER=prompt_based requires a real "
                    "LLM_PROVIDER such as 'anthropic' or another concrete "
                    "non-mock provider. Inject a test provider explicitly in "
                    "unit tests; env-based mock fallback is forbidden "
                    "(DESIGN.md R10)."
                )
            provider = create_llm_provider(llm_settings, logger=log)
        else:
            provider = llm_provider
        return PromptBasedCADCoderAgent(
            provider=provider,
            adapter_label=selected.adapter_label,
        )

    raise CADCoderAgentConfigError(
        f"CAD coder agent adapter '{selected.adapter}' is selected but no "
        "concrete adapter exists yet. Set CAD_CODER_AGENT_ADAPTER=mock for "
        "deterministic tests, or implement the adapter first."
    )
