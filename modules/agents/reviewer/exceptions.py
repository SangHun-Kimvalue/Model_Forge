class ReviewerAgentError(RuntimeError):
    """Base class for reviewer agent failures."""


class ReviewerAgentConfigError(ReviewerAgentError):
    """Reviewer adapter selection or configuration is missing or unsupported."""
