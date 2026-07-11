"""Canonical event helpers shared by every factory.

Phase 3 (PHASES.md) requires that the "mock selected" WARN event be
verifiable as JSON fields, not just a substring in a log line. To make that
testable the helper here is the single producer of the payload — every
factory (LLM, Organic Generator, Planner, CAD Coder, Reviewer, Self-Healer)
must route through ``mock_selected_extra`` so the schema cannot drift.
"""

from typing import Final

from modules.observability.trace import current_trace_fields

MOCK_SELECTED_EVENT_SUFFIX: Final = "mock_selected"


def mock_selected_event(component: str) -> str:
    """Return the canonical ``<component>.mock_selected`` event name."""
    return f"{component}.{MOCK_SELECTED_EVENT_SUFFIX}"


def mock_selected_extra(
    *,
    component: str,
    adapter: str,
    label: str,
) -> dict[str, str]:
    """Return the canonical ``extra=`` payload for a mock-selected WARN log.

    The payload always carries::

        event:     "<component>.mock_selected"
        component: <component>          # e.g. "llm_provider", "planner_agent"
        adapter:   <adapter>            # e.g. "mock"
        adapter_label: <label>          # human-friendly identifier

    Active trace fields (``trace_id`` / ``session_id`` / ``job_id``) are
    merged automatically so factories called inside an orchestrator request
    inherit the binding without changing their signatures.
    """
    payload: dict[str, str] = {
        "event": mock_selected_event(component),
        "component": component,
        "adapter": adapter,
        "adapter_label": label,
    }
    payload.update(current_trace_fields())
    return payload
