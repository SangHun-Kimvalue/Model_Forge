import logging
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from modules.agents.planner.adapters.mock import MockPlannerAgent
from modules.agents.planner.adapters.prompt_based import PromptBasedPlannerAgent
from modules.agents.planner.base import BasePlannerAgent
from modules.agents.planner.exceptions import PlannerAgentConfigError
from modules.llm.base import BaseLLMProvider
from modules.llm.factory import LLMProviderSettings, create_llm_provider
from modules.observability.events import mock_selected_event, mock_selected_extra

PlannerAdapterName = Literal["mock", "prompt_based"]


class PlannerAgentSettings(BaseModel):
    """Runtime settings for selecting a planner adapter.

    Preconditions:
        adapter must name the adapter the caller actually intends to use.
        The factory never silently downgrades to mock (R10 silent-fallback
        defense).
    Raises:
        PlannerAgentConfigError when the selected adapter has no concrete
        implementation in this build.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    adapter: PlannerAdapterName = Field(default="mock")
    adapter_label: str | None = Field(default=None)

    @classmethod
    def from_env(
        cls, environ: dict[str, str] | None = None
    ) -> "PlannerAgentSettings":
        source = environ if environ is not None else os.environ
        return cls(
            adapter=source.get("PLANNER_AGENT_ADAPTER", "mock"),
            adapter_label=source.get("PLANNER_AGENT_LABEL"),
        )


def create_planner_agent(
    settings: PlannerAgentSettings | None = None,
    logger: logging.Logger | None = None,
    *,
    llm_provider: BaseLLMProvider | None = None,
) -> BasePlannerAgent:
    selected = settings or PlannerAgentSettings.from_env()
    log = logger or logging.getLogger(__name__)

    if selected.adapter == "mock":
        label = selected.adapter_label or MockPlannerAgent.default_adapter_name
        log.warning(
            mock_selected_event("planner_agent"),
            extra=mock_selected_extra(
                component="planner_agent",
                adapter="mock",
                label=label,
            ),
        )
        return MockPlannerAgent(adapter_label=selected.adapter_label)

    if selected.adapter == "prompt_based":
        if llm_provider is None:
            llm_settings = LLMProviderSettings.from_env()
            if llm_settings.provider == "mock":
                raise PlannerAgentConfigError(
                    "PLANNER_AGENT_ADAPTER=prompt_based requires a real "
                    "LLM_PROVIDER such as 'gemini', 'anthropic', or 'openai'. "
                    "Inject a test provider explicitly in unit tests; "
                    "env-based mock fallback is forbidden (DESIGN.md R10)."
                )
            provider = create_llm_provider(llm_settings, logger=log)
        else:
            provider = llm_provider
        return PromptBasedPlannerAgent(
            provider=provider,
            adapter_label=selected.adapter_label,
        )

    raise PlannerAgentConfigError(
        f"Planner agent adapter '{selected.adapter}' is selected but no "
        "concrete adapter exists yet. Set PLANNER_AGENT_ADAPTER=mock for "
        "deterministic tests, or implement the adapter first."
    )
