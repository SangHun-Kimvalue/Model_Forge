import hashlib

from modules.organic_generator.base import BaseOrganicGenerator
from modules.organic_generator.exceptions import GenerationError
from modules.organic_generator.schemas import GenerationRequest, GenerationResult


class MockOrganicGenerator(BaseOrganicGenerator):
    """Deterministic text-to-3D adapter for unit tests and local scaffolding.

    Writes a minimal valid ASCII STL / OBJ file derived from the prompt so
    downstream stages (validator, slicer) can still operate on a real file
    handle without depending on any external vendor.
    """

    default_adapter_name = "mock-organic-v1"

    def __init__(self, adapter_label: str | None = None) -> None:
        self._adapter_label = adapter_label or self.default_adapter_name

    @property
    def provider_name(self) -> str:
        return "mock"

    def health_check(self) -> bool:
        return True

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        if not request.output_dir.exists():
            raise GenerationError(
                f"output_dir '{request.output_dir}' does not exist; "
                "callers must create the session directory before invoking the generator."
            )
        if not request.output_dir.is_dir():
            raise GenerationError(
                f"output_dir '{request.output_dir}' exists but is not a directory."
            )

        digest = _deterministic_digest(request)
        filename = f"mock-{digest}.{request.format}"
        path = request.output_dir / filename

        if request.format == "stl":
            payload = _deterministic_stl(digest)
        else:
            payload = _deterministic_obj(digest)

        path.write_text(payload, encoding="utf-8")

        return GenerationResult(
            path=path,
            format=request.format,
            adapter_used=self._adapter_label,
            generation_time_s=0.0,
            metadata={
                "deterministic": True,
                "digest": digest,
                "quality": request.quality.value,
            },
        )


def _deterministic_digest(request: GenerationRequest) -> str:
    payload = "\n".join(
        [
            request.prompt,
            request.session_id,
            request.format,
            request.quality.value,
            "" if request.seed is None else str(request.seed),
            request.negative_prompt or "",
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _deterministic_stl(digest: str) -> str:
    return (
        f"solid mock-{digest}\n"
        "  facet normal 0 0 1\n"
        "    outer loop\n"
        "      vertex 0 0 0\n"
        "      vertex 1 0 0\n"
        "      vertex 0 1 0\n"
        "    endloop\n"
        "  endfacet\n"
        f"endsolid mock-{digest}\n"
    )


def _deterministic_obj(digest: str) -> str:
    return (
        f"# mock-{digest}\n"
        "v 0.0 0.0 0.0\n"
        "v 1.0 0.0 0.0\n"
        "v 0.0 1.0 0.0\n"
        "f 1 2 3\n"
    )
