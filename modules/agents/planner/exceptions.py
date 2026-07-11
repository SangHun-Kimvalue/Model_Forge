class PlannerAgentError(RuntimeError):
    """Base class for planner agent failures."""


class PlannerAgentConfigError(PlannerAgentError):
    """Planner adapter selection or configuration is missing or unsupported."""
