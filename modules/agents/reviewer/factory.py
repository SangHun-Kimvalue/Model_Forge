import logging
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from modules.agents.reviewer.adapters.mock import MockReviewerAgent
from modules.agents.reviewer.base import BaseReviewerAgent
from modules.agents.reviewer.exceptions import ReviewerAgentConfigError
from modules.observability.events import mock_selected_event, mock_selected_extra

ReviewerAdapterName = Literal["mock", "prompt_based"]


class ReviewerAgentSettings(BaseModel):
    """Runtime settings for selecting a reviewer adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    adapter: ReviewerAdapterName = Field(default="mock")
    adapter_label: str | None = Field(default=None)

    @classmethod
    def from_env(
        cls, environ: dict[str, str] | None = None
    ) -> "ReviewerAgentSettings":
        source = environ if environ is not None else os.environ
        return cls(
            adapter=source.get("REVIEWER_AGENT_ADAPTER", "mock"),
            adapter_label=source.get("REVIEWER_AGENT_LABEL"),
        )


def create_reviewer_agent(
    settings: ReviewerAgentSettings | None = None,
    logger: logging.Logger | None = None,
) -> BaseReviewerAgent:
    selected = settings or ReviewerAgentSettings.from_env()
    log = logger or logging.getLogger(__name__)

    if selected.adapter == "mock":
        label = selected.adapter_label or MockReviewerAgent.default_adapter_name
        log.warning(
            mock_selected_event("reviewer_agent"),
            extra=mock_selected_extra(
                component="reviewer_agent",
                adapter="mock",
                label=label,
            ),
        )
        return MockReviewerAgent(adapter_label=selected.adapter_label)

    raise ReviewerAgentConfigError(
        f"Reviewer agent adapter '{selected.adapter}' is selected but no "
        "concrete adapter exists yet. Set REVIEWER_AGENT_ADAPTER=mock for "
        "deterministic tests, or implement the adapter first."
    )
