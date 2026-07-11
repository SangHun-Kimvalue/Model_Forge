class SelfHealerAgentError(RuntimeError):
    """Base class for self-healer agent failures."""


class SelfHealerAgentConfigError(SelfHealerAgentError):
    """Self-healer adapter selection or configuration is missing or unsupported."""
