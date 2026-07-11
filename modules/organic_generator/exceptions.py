class OrganicGeneratorError(RuntimeError):
    """Base class for organic (text-to-3D) generator failures."""


class OrganicGeneratorConfigError(OrganicGeneratorError):
    """Adapter selection or configuration is missing or unsupported."""


class GenerationError(OrganicGeneratorError):
    """The adapter ran but failed to produce a valid mesh."""


class GenerationTimeout(OrganicGeneratorError):
    """The adapter did not finish within the configured timeout."""
