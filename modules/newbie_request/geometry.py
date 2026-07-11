from __future__ import annotations

import math
import re
import struct
from pathlib import Path

from modules.newbie_request.printability import (
    PrintabilityReport,
    printability_gcode_metadata,
    printability_manifest_metadata,
)
from modules.newbie_request.schemas import (
    AssemblySpec,
    L1FilterReport,
    L2GeometryReport,
    RenderedAssembly,
    RouteSelectionResult,
    VisualQualityStatus,
)
from modules.newbie_request.visual_quality import visual_quality_metadata

_VERTEX_RE = re.compile(
    r"^\s*vertex\s+"
    r"(?P<x>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s+"
    r"(?P<y>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s+"
    r"(?P<z>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*$",
    re.MULTILINE,
)

_Point = tuple[float, float, float]


def evaluate_assembly_stl(
    stl_path: Path,
    *,
    spec: AssemblySpec | None = None,
    rendered: RenderedAssembly | None = None,
) -> L2GeometryReport:
    """Collect lightweight STL evidence for Phase 12D assembly smoke tests.

    This deliberately avoids claiming visual intent quality.  It only records
    cheap geometry facts that can be trusted before a renderer/VLM/manual judge
    exists.
    """

    if not stl_path.exists():
        return L2GeometryReport(
            passed=False,
            stl_exists=False,
            stl_size_bytes=0,
            metadata={"failure": "stl_missing"},
        )
    if not stl_path.is_file():
        return L2GeometryReport(
            passed=False,
            stl_exists=True,
            stl_size_bytes=0,
            metadata={"failure": "stl_path_is_not_file"},
        )

    size_bytes = stl_path.stat().st_size
    vertices, triangle_count, parser = _read_stl_vertices(stl_path)
    extents = _extents(vertices)
    volume = _volume_mm3(vertices)
    raised_detail_height = _raised_detail_height(extents, spec)
    keyring_hole_candidate = _keyring_hole_candidate(spec, rendered)

    passed = (
        size_bytes > 0
        and triangle_count > 0
        and len(vertices) > 0
        and extents is not None
        and all(value > 0 for value in extents)
    )

    return L2GeometryReport(
        passed=passed,
        stl_exists=True,
        stl_size_bytes=size_bytes,
        extents_mm=extents,
        volume_mm3=volume,
        watertight=None,
        triangle_count=triangle_count,
        vertex_count=len({_rounded_vertex(vertex) for vertex in vertices}),
        connected_component_count=None,
        keyring_hole_candidate=keyring_hole_candidate,
        raised_detail_height_mm=raised_detail_height,
        visual_quality_status=VisualQualityStatus.NOT_EVALUATED,
        metadata={
            "parser": parser,
            "l2_smoke_passed": passed,
            "geometry_presence_passed": passed,
            "watertight": "not_evaluated",
            "connected_components": "not_evaluated",
            "visual_quality_boundary": "not_evaluated",
            "keyring_hole_evidence": "source_candidate_only",
        },
    )


def assembly_manifest_metadata(
    *,
    spec: AssemblySpec,
    rendered: RenderedAssembly,
    l1_report: L1FilterReport,
    l2_report: L2GeometryReport,
    printability_report: PrintabilityReport | None = None,
    route_selection: RouteSelectionResult | None = None,
    gcode_stage: str = "not_run",
    gcode_reason: str = "phase_12d_1_stl_smoke_only",
) -> dict[str, object]:
    return {
        "source_catalog_id": spec.source_catalog_id,
        "route": "component_assembly",
        "renderer": rendered.renderer_name,
        "renderer_version": rendered.renderer_version,
        "route_selection": route_selection.model_dump(mode="json")
        if route_selection is not None
        else None,
        "renderer_metadata": dict(rendered.metadata),
        "l1_filter": {
            "passed": l1_report.passed,
            "violations": tuple(violation.model_dump() for violation in l1_report.violations),
        },
        "l2_geometry": l2_report.model_dump(mode="json"),
        "printability": printability_manifest_metadata(printability_report),
        **visual_quality_metadata(
            required=spec.quality_policy.visual_quality_required,
            required_features=spec.required_features,
        ),
        "gcode": {
            **printability_gcode_metadata(
                printability_report,
                requested_stage=gcode_stage,
                requested_reason=gcode_reason,
            )
        },
    }


def _read_stl_vertices(path: Path) -> tuple[list[_Point], int, str]:
    data = path.read_bytes()
    binary = _read_binary_stl_vertices(data)
    if binary is not None:
        vertices, triangle_count = binary
        return vertices, triangle_count, "binary_stl"

    text = data.decode("utf-8", errors="ignore")
    vertices = [
        (
            float(match.group("x")),
            float(match.group("y")),
            float(match.group("z")),
        )
        for match in _VERTEX_RE.finditer(text)
    ]
    triangle_count = len(vertices) // 3
    return vertices, triangle_count, "ascii_stl"


def _read_binary_stl_vertices(data: bytes) -> tuple[list[_Point], int] | None:
    if len(data) < 84:
        return None
    triangle_count = struct.unpack_from("<I", data, 80)[0]
    expected_size = 84 + triangle_count * 50
    if expected_size != len(data):
        return None

    vertices: list[_Point] = []
    offset = 84
    for _ in range(triangle_count):
        values = struct.unpack_from("<12f", data, offset)
        vertices.extend(
            (
                (values[3], values[4], values[5]),
                (values[6], values[7], values[8]),
                (values[9], values[10], values[11]),
            )
        )
        offset += 50
    return vertices, triangle_count


def _extents(vertices: list[_Point]) -> tuple[float, float, float] | None:
    if not vertices:
        return None
    xs = [vertex[0] for vertex in vertices]
    ys = [vertex[1] for vertex in vertices]
    zs = [vertex[2] for vertex in vertices]
    return (
        max(xs) - min(xs),
        max(ys) - min(ys),
        max(zs) - min(zs),
    )


def _volume_mm3(vertices: list[_Point]) -> float | None:
    if len(vertices) < 3:
        return None
    volume = 0.0
    for index in range(0, len(vertices) - 2, 3):
        a = vertices[index]
        b = vertices[index + 1]
        c = vertices[index + 2]
        volume += _dot(a, _cross(b, c)) / 6.0
    return math.fabs(volume)


def _raised_detail_height(
    extents: tuple[float, float, float] | None,
    spec: AssemblySpec | None,
) -> float | None:
    if extents is None or spec is None:
        return None
    return max(0.0, extents[2] - spec.size_mm.height)


def _keyring_hole_candidate(
    spec: AssemblySpec | None,
    rendered: RenderedAssembly | None,
) -> bool | None:
    if spec is None or rendered is None or "keyring_hole" not in spec.required_features:
        return None
    source = rendered.source_code.lower()
    return "difference()" in source and "cylinder" in source


def _rounded_vertex(vertex: _Point) -> _Point:
    return (round(vertex[0], 5), round(vertex[1], 5), round(vertex[2], 5))


def _dot(a: _Point, b: _Point) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: _Point, b: _Point) -> _Point:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


__all__ = [
    "assembly_manifest_metadata",
    "evaluate_assembly_stl",
]
