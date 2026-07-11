import hashlib

from modules.agents.cad_coder.base import BaseCADCoderAgent
from modules.agents.cad_coder.schemas import CADCoderRequest, CADCoderResponse


class MockCADCoderAgent(BaseCADCoderAgent):
    """Deterministic CAD coder for unit tests and local scaffolding.

    Emits a minimal, syntactically representative snippet for the requested
    DSL so downstream sandbox runner contract tests can exercise the full
    request-shape without depending on a real LLM.
    """

    default_adapter_name = "mock-cad-coder-v1"

    def __init__(self, adapter_label: str | None = None) -> None:
        self._adapter_label = adapter_label or self.default_adapter_name

    @property
    def adapter_name(self) -> str:
        return self._adapter_label

    async def generate_code(self, request: CADCoderRequest) -> CADCoderResponse:
        digest = _digest(request)
        code = _snippet_for(request.dsl, digest, request.description)
        return CADCoderResponse(
            subtask_id=request.subtask_id,
            dsl=request.dsl,
            code=code,
            entrypoint="main",
            trace_id=request.trace_id,
        )


def _digest(request: CADCoderRequest) -> str:
    payload = f"{request.subtask_id}\n{request.dsl}\n{request.description}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def _snippet_for(dsl: str, digest: str, description: str) -> str:
    safe_desc = description.replace("\n", " ").strip()
    if dsl == "cadquery":
        return (
            f"# mock cadquery {digest} — {safe_desc}\n"
            "import cadquery as cq\n\n"
            "def main():\n"
            "    return cq.Workplane('XY').box(10, 10, 10)\n"
        )
    if dsl == "build123d":
        return (
            f"# mock build123d {digest} — {safe_desc}\n"
            "from build123d import Box\n\n"
            "def main():\n"
            "    return Box(10, 10, 10)\n"
        )
    return (
        f"// mock openscad {digest} — {safe_desc}\n"
        "module main() { cube([10, 10, 10]); }\n"
        "main();\n"
    )
