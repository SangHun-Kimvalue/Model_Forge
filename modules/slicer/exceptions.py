"""Slicer error taxonomy (DESIGN.md §P8, §4.5).

The self-healing loop reacts to the concrete subclass: ``SlicerExecutionError``
goes back into retry, ``SlicerVersionError`` / ``UnsupportedSchemaVersionError``
abort the run because they indicate the pinned environment drifted.
"""


class SlicerError(RuntimeError):
    """Base class for slicer failures."""


class SlicerConfigError(SlicerError):
    """Adapter selection, profile, or binary path is missing or invalid."""


class SlicerExecutionError(SlicerError):
    """The slicer ran but exited non-zero or produced no G-code."""


class SlicerVersionError(SlicerError):
    """The slicer binary version does not match the pinned version (R8)."""


class ThreeMFSchemaError(SlicerError):
    """The 3MF archive could not be parsed against the expected schema."""


class UnsupportedSchemaVersionError(ThreeMFSchemaError):
    """The 3MF schema version is not the one pinned by this build (R8)."""


__all__ = [
    "SlicerConfigError",
    "SlicerError",
    "SlicerExecutionError",
    "SlicerVersionError",
    "ThreeMFSchemaError",
    "UnsupportedSchemaVersionError",
]
