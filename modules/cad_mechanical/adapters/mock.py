import hashlib

from modules.cad_mechanical.base import BaseMechanicalCADGenerator
from modules.cad_mechanical.exceptions import GenerationError
from modules.cad_mechanical.schemas import (
    GenerationRequest,
    GenerationResult,
)


class MockMechanicalCADGenerator(BaseMechanicalCADGenerator):
    """Deterministic mechanical CAD adapter for unit tests and local runs.

    Does not execute the script. Hashes ``(code, dialect, session_id,
    format, seed)`` and writes a minimal valid STL / STEP stub so the
    downstream validator/slicer can operate against a real file handle.

    This is the only adapter that is allowed to skip the sandbox: it never
    interprets ``request.code``, so there is no RCE surface to defend.
    Every future real adapter MUST go through a ``SandboxRunner``.
    """

    default_adapter_name = "mock-cad-mechanical-v1"

    def __init__(self, adapter_label: str | None = None) -> None:
        self._adapter_label = adapter_label or self.default_adapter_name

    @property
    def adapter_name(self) -> str:
        return self._adapter_label

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
        filename = f"mock-mech-{digest}.{request.format}"
        path = request.output_dir / filename

        if request.format == "stl":
            payload = _deterministic_stl(digest)
        else:
            payload = _deterministic_step(digest)

        path.write_text(payload, encoding="utf-8")

        return GenerationResult(
            path=path,
            format=request.format,
            dialect=request.dialect,
            adapter_used=self._adapter_label,
            generation_time_s=0.0,
            metadata={
                "deterministic": True,
                "digest": digest,
                "dialect": request.dialect.value,
                "sandboxed": False,
                "reason": "mock adapter does not execute request.code",
            },
        )


def _deterministic_digest(request: GenerationRequest) -> str:
    payload = "\n".join(
        [
            request.code,
            request.dialect.value,
            request.session_id,
            request.format,
            "" if request.seed is None else str(request.seed),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _deterministic_stl(digest: str) -> str:
    """Emit a closed-manifold unit cube STL.

    Phase 8B: the previous single-triangle stub forced the orchestrator
    pipeline to skip MANIFOLD validation. A real closed cube (12
    triangles, watertight) lets the validator's manifold check run
    against the mock adapter so the contract is exercised end-to-end.

    Vertices are placed at integer coordinates on [0, 10] so the
    bounding-box dimension (10 mm) clears the default
    ``min_wall_thickness_mm`` of 0.8 mm.
    """
    triangles = _CUBE_TRIANGLES
    lines = [f"solid mock-mech-{digest}"]
    for normal, (a, b, c) in triangles:
        nx, ny, nz = normal
        lines.append(f"  facet normal {nx} {ny} {nz}")
        lines.append("    outer loop")
        for vx, vy, vz in (a, b, c):
            lines.append(f"      vertex {vx} {vy} {vz}")
        lines.append("    endloop")
        lines.append("  endfacet")
    lines.append(f"endsolid mock-mech-{digest}")
    return "\n".join(lines) + "\n"


# 12 triangles of a unit cube on [0, S] with outward-facing normals.
# Edges shared by exactly two triangles → watertight under
# ``MockMeshValidator._parse_ascii_stl``.
_S = 10.0
_V = {
    "000": (0.0, 0.0, 0.0),
    "S00": (_S, 0.0, 0.0),
    "0S0": (0.0, _S, 0.0),
    "SS0": (_S, _S, 0.0),
    "00S": (0.0, 0.0, _S),
    "S0S": (_S, 0.0, _S),
    "0SS": (0.0, _S, _S),
    "SSS": (_S, _S, _S),
}
_CUBE_TRIANGLES: tuple[
    tuple[
        tuple[float, float, float],
        tuple[
            tuple[float, float, float],
            tuple[float, float, float],
            tuple[float, float, float],
        ],
    ],
    ...,
] = (
    # bottom (z = 0), normal -z
    ((0.0, 0.0, -1.0), (_V["000"], _V["0S0"], _V["SS0"])),
    ((0.0, 0.0, -1.0), (_V["000"], _V["SS0"], _V["S00"])),
    # top (z = S), normal +z
    ((0.0, 0.0, 1.0), (_V["00S"], _V["S0S"], _V["SSS"])),
    ((0.0, 0.0, 1.0), (_V["00S"], _V["SSS"], _V["0SS"])),
    # front (y = 0), normal -y
    ((0.0, -1.0, 0.0), (_V["000"], _V["S00"], _V["S0S"])),
    ((0.0, -1.0, 0.0), (_V["000"], _V["S0S"], _V["00S"])),
    # back (y = S), normal +y
    ((0.0, 1.0, 0.0), (_V["0S0"], _V["0SS"], _V["SSS"])),
    ((0.0, 1.0, 0.0), (_V["0S0"], _V["SSS"], _V["SS0"])),
    # left (x = 0), normal -x
    ((-1.0, 0.0, 0.0), (_V["000"], _V["00S"], _V["0SS"])),
    ((-1.0, 0.0, 0.0), (_V["000"], _V["0SS"], _V["0S0"])),
    # right (x = S), normal +x
    ((1.0, 0.0, 0.0), (_V["S00"], _V["SS0"], _V["SSS"])),
    ((1.0, 0.0, 0.0), (_V["S00"], _V["SSS"], _V["S0S"])),
)


def _deterministic_step(digest: str) -> str:
    return (
        "ISO-10303-21;\n"
        "HEADER;\n"
        f"FILE_DESCRIPTION(('mock-mech-{digest}'),'2;1');\n"
        f"FILE_NAME('mock-mech-{digest}.step','',(''),(''), 'mock','mock','');\n"
        "FILE_SCHEMA(('AUTOMOTIVE_DESIGN'));\n"
        "ENDSEC;\n"
        "DATA;\n"
        "ENDSEC;\n"
        "END-ISO-10303-21;\n"
    )


__all__ = ["MockMechanicalCADGenerator"]
