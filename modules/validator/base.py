from abc import ABC, abstractmethod

from modules.validator.schemas import (
    RepairRequest,
    RepairResult,
    ValidationReport,
    ValidationRequest,
)


class BaseMeshValidator(ABC):
    """Contract for mesh validators (DESIGN.md §4.6, Validation Node).

    Preconditions:
        ``request.mesh_path`` exists and is readable. The caller has
        chosen the checks to run; the validator does not silently skip
        unsupported checks — it raises ``UnsupportedFormatError`` instead
        (R10, no silent fallback).
    Postconditions:
        ``ValidationReport.passed`` is consistent with the issues
        (enforced by the schema). The report is the single source of
        truth — the validator must never raise for content-level
        failures (non-manifold, thin walls); those become issues.
    Raises:
        ``ValidatorError`` subclasses for IO failures, unsupported
        formats, or adapter configuration problems.

    Note:
        Validation and repair are kept on separate ABCs (P12). A validator
        is read-only; a repairer mutates the mesh and writes a new file.
    """

    @property
    @abstractmethod
    def adapter_name(self) -> str:
        """Stable adapter identifier used in logs, traces, and tests."""

    @abstractmethod
    async def validate(self, request: ValidationRequest) -> ValidationReport:
        """Inspect ``request.mesh_path`` and return a ``ValidationReport``."""

    @abstractmethod
    def health_check(self) -> bool:
        """Return True if the adapter is usable in the current environment.

        Implementations must not raise; missing trimesh / pymeshlab
        installs must be reflected as False so the factory can route
        around unhealthy adapters without exception handling.
        """

    async def __aenter__(self) -> "BaseMeshValidator":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:  # noqa: B027 — intentional no-op default
        """Release adapter-owned resources. Default: no-op."""


class BaseMeshRepairer(ABC):
    """Contract for mesh repairers (DESIGN.md §4.6, repair branch).

    A repairer is a *write* operation: it consumes a candidate mesh and
    emits a new file. The validator's report drives whether a repair is
    attempted; the repairer never re-validates on its own — that's the
    orchestrator's job to keep the validate→repair→validate loop visible
    in traces.

    Raises:
        ``RepairError`` if the underlying tool failed to read or write
        the mesh. Successful repairs that *still* leave the mesh
        defective return normally; the next validation pass will catch
        the residual issues.
    """

    @property
    @abstractmethod
    def adapter_name(self) -> str:
        """Stable adapter identifier used in logs, traces, and tests."""

    @abstractmethod
    async def repair(self, request: RepairRequest) -> RepairResult:
        """Apply best-effort repairs and write the result to ``output_dir``."""

    @abstractmethod
    def health_check(self) -> bool:
        """Return True if the repairer can run in this environment."""


__all__ = ["BaseMeshRepairer", "BaseMeshValidator"]
