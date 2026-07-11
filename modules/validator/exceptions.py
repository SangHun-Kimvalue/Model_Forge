"""Validator error taxonomy (DESIGN.md §P8, §4.6).

The validator returns a ``ValidationReport`` for *content* failures
(non-manifold, thin walls). Exceptions are reserved for *infrastructure*
failures the caller cannot recover from by reading the report.
"""


class ValidatorError(RuntimeError):
    """Base class for validator infrastructure failures."""


class ValidatorConfigError(ValidatorError):
    """Adapter selection or configuration is missing or unsupported."""


class ValidatorIOError(ValidatorError):
    """The validator could not read the mesh file from disk."""


class UnsupportedFormatError(ValidatorError):
    """The adapter does not handle the requested ``mesh_format``."""


class RepairError(ValidatorError):
    """The repair pass failed mechanically (parser crash, write error)."""


__all__ = [
    "RepairError",
    "UnsupportedFormatError",
    "ValidatorConfigError",
    "ValidatorError",
    "ValidatorIOError",
]
