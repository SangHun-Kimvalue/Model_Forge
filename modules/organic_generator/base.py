from abc import ABC, abstractmethod

from modules.organic_generator.schemas import GenerationRequest, GenerationResult


class BaseOrganicGenerator(ABC):
    """Contract for text-to-3D organic mesh generators.

    Preconditions:
        request.output_dir already exists and is writable by the process.
        Provider-specific options (model id, API keys) are bound at
        construction time, not on every call.
    Postconditions:
        The returned GenerationResult.path points to a finalized mesh file in
        the requested format. adapter_used matches provider_name or a more
        specific label for that adapter instance.
    Raises:
        OrganicGeneratorError subclasses for configuration, generation, and
        timeout failures.
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Stable adapter identifier used in logs, traces, and tests."""

    @abstractmethod
    async def generate(self, request: GenerationRequest) -> GenerationResult:
        """Generate a 3D mesh from a natural-language prompt."""

    @abstractmethod
    def health_check(self) -> bool:
        """Return True if the adapter is usable in the current environment.

        Implementations must not raise; missing credentials, GPUs, or model
        weights must be reflected as False so the factory can route around
        unhealthy adapters without exception handling at the call site.
        """

    async def __aenter__(self) -> "BaseOrganicGenerator":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:  # noqa: B027 — intentional no-op default
        """Release adapter-owned resources. Default: no-op."""
