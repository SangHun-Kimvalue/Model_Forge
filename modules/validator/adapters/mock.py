"""Deterministic mock validator + repairer (DESIGN.md §4.6).

These adapters are pure-Python and parse ASCII STL / OBJ without any
optional dependency (trimesh, pymeshlab) so the harness can run on a
fresh checkout. They are good enough to validate the *contract* —
manifold/wall-thickness/repair plumbing — not the geometry. A real
adapter (trimesh-backed) lands in a later phase.
"""

import hashlib
import math
import re
import time

from modules.validator.base import BaseMeshRepairer, BaseMeshValidator
from modules.validator.exceptions import (
    RepairError,
    UnsupportedFormatError,
    ValidatorIOError,
)
from modules.validator.schemas import (
    IssueSeverity,
    MeshFormat,
    RepairRequest,
    RepairResult,
    ValidationCheck,
    ValidationIssue,
    ValidationReport,
    ValidationRequest,
)

_SUPPORTED_FORMATS: frozenset[MeshFormat] = frozenset({"stl", "obj"})


class _MeshSnapshot:
    __slots__ = ("triangle_count", "edges", "bbox_min", "bbox_max", "has_normals")

    def __init__(
        self,
        triangle_count: int,
        edges: dict[tuple[tuple[float, float, float], tuple[float, float, float]], int],
        bbox_min: tuple[float, float, float] | None,
        bbox_max: tuple[float, float, float] | None,
        has_normals: bool,
    ) -> None:
        self.triangle_count = triangle_count
        self.edges = edges
        self.bbox_min = bbox_min
        self.bbox_max = bbox_max
        self.has_normals = has_normals

    @property
    def is_watertight(self) -> bool:
        if self.triangle_count < 4:
            return False
        return all(count == 2 for count in self.edges.values())

    def min_bbox_dimension_mm(self) -> float | None:
        if self.bbox_min is None or self.bbox_max is None:
            return None
        return min(b - a for a, b in zip(self.bbox_min, self.bbox_max, strict=True))


class MockMeshValidator(BaseMeshValidator):
    """Pure-Python validator over ASCII STL / OBJ for harness purposes."""

    default_adapter_name = "mock-validator-v1"

    def __init__(self, adapter_label: str | None = None) -> None:
        self._adapter_label = adapter_label or self.default_adapter_name

    @property
    def adapter_name(self) -> str:
        return self._adapter_label

    def health_check(self) -> bool:
        return True

    async def validate(self, request: ValidationRequest) -> ValidationReport:
        if request.mesh_format not in _SUPPORTED_FORMATS:
            raise UnsupportedFormatError(
                f"MockMeshValidator does not handle mesh_format='{request.mesh_format}'. "
                f"Supported: {sorted(_SUPPORTED_FORMATS)}. STEP requires a CAD-aware adapter."
            )
        if not request.mesh_path.exists():
            raise ValidatorIOError(
                f"mesh_path '{request.mesh_path}' does not exist."
            )
        if not request.mesh_path.is_file():
            raise ValidatorIOError(
                f"mesh_path '{request.mesh_path}' is not a regular file."
            )

        start = time.perf_counter()
        try:
            text = request.mesh_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValidatorIOError(
                f"failed to read mesh_path '{request.mesh_path}': {exc}"
            ) from exc

        snapshot = (
            _parse_ascii_stl(text)
            if request.mesh_format == "stl"
            else _parse_ascii_obj(text)
        )

        issues: list[ValidationIssue] = []

        if ValidationCheck.MANIFOLD in request.checks:
            if snapshot.triangle_count == 0:
                issues.append(
                    ValidationIssue(
                        check=ValidationCheck.MANIFOLD,
                        severity=IssueSeverity.ERROR,
                        message="mesh contains no triangles",
                    )
                )
            elif snapshot.triangle_count < 4:
                issues.append(
                    ValidationIssue(
                        check=ValidationCheck.MANIFOLD,
                        severity=IssueSeverity.ERROR,
                        message=(
                            f"mesh has {snapshot.triangle_count} triangles; "
                            "a closed manifold needs at least 4 (tetrahedron)."
                        ),
                        location={"triangle_count": snapshot.triangle_count},
                    )
                )
            elif not snapshot.is_watertight:
                non_paired = [
                    edge for edge, count in snapshot.edges.items() if count != 2
                ]
                issues.append(
                    ValidationIssue(
                        check=ValidationCheck.MANIFOLD,
                        severity=IssueSeverity.ERROR,
                        message=(
                            f"mesh is not watertight: {len(non_paired)} edges are "
                            "not shared by exactly two triangles."
                        ),
                        location={"non_paired_edge_count": len(non_paired)},
                    )
                )

        if ValidationCheck.WALL_THICKNESS in request.checks:
            min_dim = snapshot.min_bbox_dimension_mm()
            if min_dim is not None and min_dim < request.min_wall_thickness_mm:
                issues.append(
                    ValidationIssue(
                        check=ValidationCheck.WALL_THICKNESS,
                        severity=IssueSeverity.WARNING,
                        message=(
                            f"bounding-box minimum dimension {min_dim:.3f} mm is "
                            f"below the required {request.min_wall_thickness_mm} mm "
                            "(mock heuristic; real check requires a sampler)."
                        ),
                        location={"min_bbox_dimension_mm": min_dim},
                    )
                )

        if ValidationCheck.NORMAL_CONSISTENCY in request.checks:
            if not snapshot.has_normals and request.mesh_format == "stl":
                issues.append(
                    ValidationIssue(
                        check=ValidationCheck.NORMAL_CONSISTENCY,
                        severity=IssueSeverity.WARNING,
                        message="no facet normals were recorded in the STL.",
                    )
                )

        has_error = any(i.severity is IssueSeverity.ERROR for i in issues)
        is_watertight = (
            snapshot.is_watertight if snapshot.triangle_count > 0 else None
        )

        return ValidationReport(
            passed=not has_error,
            mesh_path=request.mesh_path,
            mesh_format=request.mesh_format,
            triangle_count=snapshot.triangle_count,
            is_watertight=is_watertight,
            issues=tuple(issues),
            adapter_used=self._adapter_label,
            duration_s=time.perf_counter() - start,
            metadata={
                "deterministic": True,
                "checks_requested": sorted(c.value for c in request.checks),
            },
        )


class MockMeshRepairer(BaseMeshRepairer):
    """Deterministic mock repairer.

    Copies the input mesh verbatim into ``output_dir`` and records the
    operations that *would* have been attempted. Real repair (hole
    filling, normal recomputation) requires pymeshlab — added in a
    later phase under its own adapter.
    """

    default_adapter_name = "mock-repairer-v1"

    def __init__(self, adapter_label: str | None = None) -> None:
        self._adapter_label = adapter_label or self.default_adapter_name

    @property
    def adapter_name(self) -> str:
        return self._adapter_label

    def health_check(self) -> bool:
        return True

    async def repair(self, request: RepairRequest) -> RepairResult:
        if not request.mesh_path.exists() or not request.mesh_path.is_file():
            raise RepairError(
                f"mesh_path '{request.mesh_path}' is missing or not a file."
            )
        if not request.output_dir.exists() or not request.output_dir.is_dir():
            raise RepairError(
                f"output_dir '{request.output_dir}' is missing or not a directory."
            )

        start = time.perf_counter()
        try:
            payload = request.mesh_path.read_bytes()
        except OSError as exc:
            raise RepairError(f"failed to read mesh: {exc}") from exc

        digest = hashlib.sha256(payload).hexdigest()[:12]
        repaired_path = (
            request.output_dir / f"repaired-{digest}.{request.mesh_format}"
        )
        try:
            repaired_path.write_bytes(payload)
        except OSError as exc:
            raise RepairError(
                f"failed to write repaired mesh to '{repaired_path}': {exc}"
            ) from exc

        return RepairResult(
            repaired_path=repaired_path,
            mesh_format=request.mesh_format,
            operations_applied=("noop-copy",),
            adapter_used=self._adapter_label,
            duration_s=time.perf_counter() - start,
            metadata={
                "deterministic": True,
                "source_digest": digest,
                "note": "mock repairer performs no geometric repair",
            },
        )


_VERTEX_RE = re.compile(
    r"vertex\s+(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
    r"\s+(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
    r"\s+(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
)
_FACET_RE = re.compile(r"facet\s+normal")
_OBJ_VERTEX_RE = re.compile(
    r"^v\s+(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
    r"\s+(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
    r"\s+(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)",
    re.MULTILINE,
)
_OBJ_FACE_RE = re.compile(r"^f\s+(.+)$", re.MULTILINE)


def _parse_ascii_stl(text: str) -> _MeshSnapshot:
    facet_count = len(_FACET_RE.findall(text))
    vertices: list[tuple[float, float, float]] = [
        (float(x), float(y), float(z)) for x, y, z in _VERTEX_RE.findall(text)
    ]
    if len(vertices) % 3 != 0:
        raise ValidatorIOError(
            f"ASCII STL has {len(vertices)} vertices, not a multiple of 3."
        )
    triangle_count = len(vertices) // 3
    edges = _edge_multiplicity(vertices)
    bbox_min, bbox_max = _bbox(vertices)
    has_normals = facet_count > 0
    return _MeshSnapshot(triangle_count, edges, bbox_min, bbox_max, has_normals)


def _parse_ascii_obj(text: str) -> _MeshSnapshot:
    obj_vertices: list[tuple[float, float, float]] = [
        (float(x), float(y), float(z))
        for x, y, z in _OBJ_VERTEX_RE.findall(text)
    ]
    triangle_vertices: list[tuple[float, float, float]] = []
    for raw_face in _OBJ_FACE_RE.findall(text):
        tokens = raw_face.split()
        if len(tokens) < 3:
            continue
        indices = [int(tok.split("/")[0]) for tok in tokens]
        resolved: list[tuple[float, float, float]] = []
        for idx in indices:
            actual = idx - 1 if idx > 0 else len(obj_vertices) + idx
            if not 0 <= actual < len(obj_vertices):
                raise ValidatorIOError(
                    f"OBJ face references missing vertex index {idx}."
                )
            resolved.append(obj_vertices[actual])
        # fan-triangulate polygons
        for i in range(1, len(resolved) - 1):
            triangle_vertices.extend([resolved[0], resolved[i], resolved[i + 1]])

    triangle_count = len(triangle_vertices) // 3
    edges = _edge_multiplicity(triangle_vertices)
    bbox_min, bbox_max = _bbox(obj_vertices or triangle_vertices)
    return _MeshSnapshot(triangle_count, edges, bbox_min, bbox_max, has_normals=False)


def _edge_multiplicity(
    vertices: list[tuple[float, float, float]],
) -> dict[tuple[tuple[float, float, float], tuple[float, float, float]], int]:
    counts: dict[
        tuple[tuple[float, float, float], tuple[float, float, float]], int
    ] = {}
    for tri in range(len(vertices) // 3):
        a, b, c = vertices[3 * tri : 3 * tri + 3]
        for u, v in ((a, b), (b, c), (c, a)):
            key = (u, v) if u <= v else (v, u)
            counts[key] = counts.get(key, 0) + 1
    return counts


def _bbox(
    vertices: list[tuple[float, float, float]],
) -> tuple[tuple[float, float, float] | None, tuple[float, float, float] | None]:
    if not vertices:
        return None, None
    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    zs = [v[2] for v in vertices]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


# math import is retained for future per-triangle normal sanity checks
_ = math


__all__ = ["MockMeshRepairer", "MockMeshValidator"]
