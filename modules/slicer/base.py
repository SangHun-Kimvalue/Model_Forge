from abc import ABC, abstractmethod

from modules.slicer.schemas import SliceRequest, SliceResult


class BaseSlicer(ABC):
    """Contract for slicers (DESIGN.md §4.5, Module D).

    Preconditions:
        ``request.mesh_path`` exists in ``request.mesh_format`` and
        ``request.output_dir`` is writable. ``printer`` and ``material``
        are pinned — the adapter does not consult external profile
        catalogs.
    Postconditions:
        ``SliceResult.gcode_path`` references a file in ``output_dir``
        and ``slicer_version`` matches the pinned version the adapter
        was built against (R8).
    Raises:
        ``SlicerError`` subclasses for configuration, execution,
        version, and 3MF schema failures. ``except Exception`` is
        forbidden (P8).

    Note:
        DESIGN.md §4.5 makes the slicer a T1 plugin: the adapter is
        chosen by printer model. The orchestrator never picks a slicer
        based on availability — that would silently change the output.
    """

    @property
    @abstractmethod
    def adapter_name(self) -> str:
        """Stable adapter identifier used in logs, traces, and tests."""

    @property
    @abstractmethod
    def slicer_version(self) -> str:
        """Pinned slicer binary version this adapter targets (R8)."""

    @abstractmethod
    async def slice(self, request: SliceRequest) -> SliceResult:
        """Run the slicer and return a populated ``SliceResult``."""

    @abstractmethod
    def health_check(self) -> bool:
        """Return True if the slicer binary / profile set is reachable.

        Implementations must not raise; a missing binary, wrong version,
        or unreadable profile must surface as False so the factory can
        fail loudly instead of swallowing exceptions at the call site.
        """

    async def __aenter__(self) -> "BaseSlicer":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:  # noqa: B027 — intentional no-op default
        """Release adapter-owned resources. Default: no-op."""


__all__ = ["BaseSlicer"]
