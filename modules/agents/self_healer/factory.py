import logging
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from modules.agents.self_healer.adapters.mock import MockSelfHealerAgent
from modules.agents.self_healer.base import BaseSelfHealerAgent
from modules.agents.self_healer.exceptions import SelfHealerAgentConfigError
from modules.observability.events import mock_selected_event, mock_selected_extra

SelfHealerAdapterName = Literal["mock", "prompt_based"]


class SelfHealerAgentSettings(BaseModel):
    """Runtime settings for selecting a self-healer adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    adapter: SelfHealerAdapterName = Field(default="mock")
    adapter_label: str | None = Field(default=None)

    @classmethod
    def from_env(
        cls, environ: dict[str, str] | None = None
    ) -> "SelfHealerAgentSettings":
        source = environ if environ is not None else os.environ
        return cls(
            adapter=source.get("SELF_HEALER_AGENT_ADAPTER", "mock"),
            adapter_label=source.get("SELF_HEALER_AGENT_LABEL"),
        )


def create_self_healer_agent(
    settings: SelfHealerAgentSettings | None = None,
    logger: logging.Logger | None = None,
) -> BaseSelfHealerAgent:
    selected = settings or SelfHealerAgentSettings.from_env()
    log = logger or logging.getLogger(__name__)

    if selected.adapter == "mock":
        label = selected.adapter_label or MockSelfHealerAgent.default_adapter_name
        log.warning(
            mock_selected_event("self_healer_agent"),
            extra=mock_selected_extra(
                component="self_healer_agent",
                adapter="mock",
                label=label,
            ),
        )
        return MockSelfHealerAgent(adapter_label=selected.adapter_label)

    raise SelfHealerAgentConfigError(
        f"Self-healer agent adapter '{selected.adapter}' is selected but no "
        "concrete adapter exists yet. Set SELF_HEALER_AGENT_ADAPTER=mock for "
        "deterministic tests, or implement the adapter first."
    )
