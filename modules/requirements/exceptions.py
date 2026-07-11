class RequirementError(Exception):
    """Base class for requirement extraction failures."""


class RequirementConfigError(RequirementError):
    """Raised when a requirement extractor is misconfigured."""
