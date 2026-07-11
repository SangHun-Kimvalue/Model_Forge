from abc import ABC, abstractmethod

from modules.cad_mechanical.schemas import GenerationRequest, GenerationResult


class BaseMechanicalCADGenerator(ABC):
    """Contract for LLM-authored DSL → 3D mechanical mesh generators.

    Preconditions:
        ``request.output_dir`` exists and is writable. The caller has
        already produced ``request.code`` from an LLM (or fixture) — this
        contract does not call the LLM itself; ``cad_coder`` agent owns
        that step (DESIGN.md §4.2.cad_coder).
    Postconditions:
        ``GenerationResult.path`` references a file in ``output_dir``
        containing a finalized mesh in the requested format.
        ``adapter_used`` carries the adapter's stable identifier so logs
        and traces can attribute the artifact to a specific implementation.
    Raises:
        ``CadMechanicalError`` subclasses for config, topology, sandbox,
        timeout, and generation failures. See ``exceptions`` module.

    Note:
        DESIGN.md §2.3 forbids a generic ``BaseAgent``. Likewise this ABC
        is the **only** abstraction over mechanical CAD adapters — we do
        not collapse mechanical and organic generation into one ABC even
        though they share a ``generate()`` shape; their preconditions
        (LLM-authored DSL vs natural-language prompt) and failure modes
        (sandbox vs vendor API) diverge.
    """

    @property
    @abstractmethod
    def adapter_name(self) -> str:
        """Stable adapter identifier used in logs, traces, and tests."""

    @abstractmethod
    async def generate(self, request: GenerationRequest) -> GenerationResult:
        """Render ``request.code`` to a mesh artifact on disk."""

    @abstractmethod
    def health_check(self) -> bool:
        """Return True if the adapter is usable in the current environment.

        Implementations must not raise; missing Docker, missing CadQuery
        install, or absent build tools must surface as False so the
        factory can route around unhealthy adapters without try/except.
        """

    async def __aenter__(self) -> "BaseMechanicalCADGenerator":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:  # noqa: B027 — intentional no-op default
        """Release adapter-owned resources. Default: no-op."""
