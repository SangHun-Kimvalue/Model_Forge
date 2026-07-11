"""OrcaSlicer adapter scaffold (DESIGN.md §4.5, R8).

The concrete OrcaSlicer integration is intentionally not implemented in
Phase 5. Instantiating this adapter raises ``SlicerConfigError`` so the
selection path is fully wired but no host process can be invoked until
the slicer image is pinned and the 3MF schema contract is validated.

A future phase will:
    - Pin the OrcaSlicer binary version in a Docker image (R8).
    - Lock the 3MF schema version against ``EXPECTED_THREEMF_SCHEMA_VERSION``.
    - Replace this scaffold's ``slice`` implementation with a subprocess
      call that mirrors ``DockerSandboxRunner`` isolation patterns.
"""

from modules.slicer.base import BaseSlicer
from modules.slicer.exceptions import SlicerConfigError
from modules.slicer.schemas import SliceRequest, SliceResult


class OrcaSlicerAdapter(BaseSlicer):
    """Placeholder for the eventual OrcaSlicer subprocess adapter."""

    default_adapter_name = "orca-slicer"
    default_slicer_version = "orca-unpinned"

    def __init__(self, *_: object, **__: object) -> None:
        raise SlicerConfigError(
            "OrcaSlicerAdapter is a Phase 5 scaffold only. Pin the Orca "
            "binary version (R8) and implement the slice() subprocess "
            "call before enabling SLICER_ADAPTER=orca."
        )

    @property
    def adapter_name(self) -> str:
        return self.default_adapter_name

    @property
    def slicer_version(self) -> str:
        return self.default_slicer_version

    async def slice(self, request: SliceRequest) -> SliceResult:
        raise SlicerConfigError(
            "OrcaSlicerAdapter.slice() is unavailable until the Orca binary "
            "and 3MF schema version are pinned."
        )

    def health_check(self) -> bool:
        return False


__all__ = ["OrcaSlicerAdapter"]
