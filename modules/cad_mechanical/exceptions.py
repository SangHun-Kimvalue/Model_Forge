"""Mechanical CAD generation error taxonomy (DESIGN.md §P8, §4.3).

The orchestrator's self-healing loop (§4.2) routes retries based on the
concrete exception class, so adapters MUST raise the most specific subclass
that applies. ``except Exception`` is forbidden at every layer above this
module: callers either handle a known subclass or let it propagate.
"""


class CadMechanicalError(RuntimeError):
    """Base class for mechanical CAD generation failures."""


class CadMechanicalConfigError(CadMechanicalError):
    """Adapter selection or configuration is missing or unsupported."""


class TopologyError(CadMechanicalError):
    """The DSL script ran but produced an invalid or non-manifold solid.

    Self-healer should retry with a revised script; re-running the same
    script unchanged is guaranteed to fail again.
    """


class SandboxError(CadMechanicalError):
    """The sandbox boundary itself failed or refused to execute the script.

    Raised when the isolation runner is unavailable, the script violates
    the import allowlist, or the sandbox refuses an operation for security
    reasons. Distinct from ``TopologyError`` because the script never
    produced a candidate solid.
    """


class SandboxTimeoutError(SandboxError):
    """The script exceeded ``SandboxLimits.timeout_s`` wall clock budget."""


class SandboxMemoryError(SandboxError):
    """The script exceeded ``SandboxLimits.max_memory_mb`` resident set."""


class GenerationError(CadMechanicalError):
    """The adapter ran end-to-end but no artifact reached the output dir."""
