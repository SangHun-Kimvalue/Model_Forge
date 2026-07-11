from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path

from modules.requirements.schemas import RequirementSpec
from modules.semantic_validator.base import BaseSemanticValidator
from modules.semantic_validator.schemas import (
    SemanticValidationReport,
    SemanticValidationRequest,
)

__all__ = ["RuleBasedSemanticValidator"]

_FEATURE_MARKERS: dict[str, tuple[str, ...]] = {
    "keyring_hole": ("keyring_hole", "ring_hole", "through_hole", "hole"),
    "bear_head": ("bear_head", "head", "곰_머리", "머리"),
    "ears": ("bear_ears", "ears", "귀"),
    "eyes": ("face_eyes", "eyes", "eye", "눈"),
    "nose_or_muzzle": ("nose", "muzzle", "snout", "코", "주둥이"),
    "body_or_paws": ("body", "paws", "paw", "몸", "발"),
    "raised_logo_or_text": ("logo", "text(", "linear_extrude", "emboss", "양각"),
    "mounting_holes": ("mounting_hole", "mounting_holes", "screw_hole", "hole"),
    "motor_mounts": ("motor_mount", "motor_mounts", "motor", "모터"),
    "central_hub": ("central_hub", "hub", "center", "중앙"),
    "arms": ("arm", "arms", "x_layout", "암"),
}

_GEOMETRY_FEATURES = frozenset(
    {
        "bear_head",
        "ears",
        "eyes",
        "nose_or_muzzle",
        "body_or_paws",
        "raised_logo_or_text",
        "mounting_holes",
        "motor_mounts",
        "central_hub",
        "arms",
    }
)
_DECORATIVE_FEATURES = frozenset(
    {
        "bear_head",
        "ears",
        "eyes",
        "nose_or_muzzle",
        "body_or_paws",
        "raised_logo_or_text",
    }
)
_MIN_NONBLANK_SPAN_MM = 5.0
_MIN_NONBLANK_Z_MM = 0.05
_MIN_FEATURE_TRIANGLES = 12
_MIN_FEATURE_VERTICES = 12
_MIN_DECORATIVE_HEIGHT_MM = 1.0
_GEOMETRY_PRIMITIVES = (
    "circle",
    "square",
    "cube",
    "cylinder",
    "sphere",
    "polygon",
    "polyhedron",
    "text(",
    "linear_extrude",
    "rotate_extrude",
    "offset",
    "hull",
)


@dataclass(frozen=True)
class _STLStats:
    extents: tuple[float, float, float]
    triangle_count: int
    vertex_count: int


@dataclass(frozen=True)
class _OpenSCADModuleIndex:
    """Small structural index for conservative OpenSCAD semantic checks."""

    code: str
    modules: dict[str, str]

    @classmethod
    def from_code(cls, code: str) -> _OpenSCADModuleIndex:
        return cls(code=code, modules=_module_bodies(code))

    def feature_has_geometry_evidence(self, markers: tuple[str, ...]) -> bool:
        if any(marker == "text(" and "text(" in self.code for marker in markers):
            return True

        for module_name in self._matching_modules(markers):
            if self.module_has_geometry_evidence(module_name):
                return True

        return False

    def module_has_geometry_evidence(
        self,
        module_name: str,
        *,
        seen: frozenset[str] = frozenset(),
    ) -> bool:
        if module_name in seen:
            return False
        body = self.modules.get(module_name)
        if body is None:
            return False
        if _contains_geometry_primitive(body):
            return True
        next_seen = seen | {module_name}
        return any(
            self.module_has_geometry_evidence(called, seen=next_seen)
            for called in self._called_modules(body)
        )

    def keyring_hole_is_cut(self) -> bool:
        difference_blocks = tuple(_call_blocks(self.code, "difference"))
        if not difference_blocks:
            return False

        hole_modules = self._matching_modules(_FEATURE_MARKERS["keyring_hole"])
        if hole_modules:
            return any(
                any(
                    _body_calls_module(block, module_name)
                    and self.module_has_geometry_evidence(module_name)
                    for module_name in hole_modules
                )
                for block in difference_blocks
            )

        return any(_block_has_generic_hole_cut(block) for block in difference_blocks)

    def decorative_feature_too_low(self, requirements: RequirementSpec) -> bool:
        decorative_required = set(requirements.required_features) & _DECORATIVE_FEATURES
        if not decorative_required:
            return False

        for feature in decorative_required:
            markers = _FEATURE_MARKERS.get(feature, (feature,))
            for module_name in self._matching_modules(markers):
                heights = self.module_linear_extrude_heights(module_name)
                if heights and max(heights) < _MIN_DECORATIVE_HEIGHT_MM:
                    return True

        if not self.modules:
            heights = _linear_extrude_heights(self.code)
            return bool(heights) and max(heights) < _MIN_DECORATIVE_HEIGHT_MM
        return False

    def module_linear_extrude_heights(
        self,
        module_name: str,
        *,
        seen: frozenset[str] = frozenset(),
    ) -> list[float]:
        if module_name in seen:
            return []
        body = self.modules.get(module_name)
        if body is None:
            return []
        heights = _linear_extrude_heights(body)
        next_seen = seen | {module_name}
        for called in self._called_modules(body):
            heights.extend(
                self.module_linear_extrude_heights(called, seen=next_seen)
            )
        return heights

    def _matching_modules(self, markers: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            module_name
            for module_name in self.modules
            if _name_matches_markers(module_name, markers)
        )

    def _called_modules(self, body: str) -> tuple[str, ...]:
        return tuple(
            name
            for name in self.modules
            if _body_calls_module(body, name)
        )


class RuleBasedSemanticValidator(BaseSemanticValidator):
    """First-pass semantic gate using conservative code markers."""

    default_adapter_name = "rule-based-semantic-validator-v1"

    def __init__(self, adapter_label: str | None = None) -> None:
        self._adapter_label = adapter_label or self.default_adapter_name

    @property
    def adapter_name(self) -> str:
        return self._adapter_label

    def validate(self, request: SemanticValidationRequest) -> SemanticValidationReport:
        requirements = request.requirements
        if requirements is None or not requirements.required_features:
            return SemanticValidationReport(passed=True, trace_id=request.trace_id)

        code = request.code or ""
        searchable = _searchable_code(code)
        missing = _missing_features(searchable, requirements)
        violations = _violated_constraints(searchable, requirements)
        stl_stats = _stl_stats(request.stl_path)
        extents = request.stl_extents_mm or (
            stl_stats.extents if stl_stats is not None else None
        )
        triangle_count = (
            request.stl_triangle_count
            if request.stl_triangle_count is not None
            else (stl_stats.triangle_count if stl_stats is not None else None)
        )
        vertex_count = (
            request.stl_vertex_count
            if request.stl_vertex_count is not None
            else (stl_stats.vertex_count if stl_stats is not None else None)
        )
        violations.extend(
            _violated_geometry_constraints(
                extents,
                triangle_count,
                vertex_count,
                requirements,
            )
        )

        if missing or violations:
            return SemanticValidationReport(
                passed=False,
                missing_features=tuple(missing),
                violated_constraints=tuple(violations),
                failure_type="missing_required_features"
                if missing
                else "constraint_violation",
                retry_hint=_retry_hint(requirements, missing, violations),
                trace_id=request.trace_id,
            )

        return SemanticValidationReport(passed=True, trace_id=request.trace_id)


def _searchable_code(code: str) -> str:
    # This first-pass gate may use identifiers/module names, but it must not
    # accept comments or string literals as feature evidence.
    return _strip_openscad_comments_and_strings(code).lower()


def _missing_features(code: str, requirements: RequirementSpec) -> list[str]:
    missing: list[str] = []
    for feature in requirements.required_features:
        if not _feature_present(code, feature):
            missing.append(feature)
    return missing


def _feature_present(code: str, feature: str) -> bool:
    if feature == "keyring_hole":
        return _OpenSCADModuleIndex.from_code(code).keyring_hole_is_cut()
    if feature == "bracket_body":
        return _bracket_body_present(code)
    if feature == "mounting_holes":
        return _mounting_holes_present(code)
    markers = _FEATURE_MARKERS.get(feature, (feature,))
    if feature in _GEOMETRY_FEATURES:
        return _feature_has_geometry_evidence(code, markers)
    return any(marker in code for marker in markers)


def _violated_constraints(code: str, requirements: RequirementSpec) -> list[str]:
    violations: list[str] = []
    if (
        "keyring_hole" in requirements.required_features
        and not _OpenSCADModuleIndex.from_code(code).keyring_hole_is_cut()
    ):
        violations.append("keyring_hole_must_be_cut_with_difference")
    if (
        "raised_logo_or_text" in requirements.required_features
        and not _has_text_or_emboss_geometry(code)
    ):
        violations.append("raised_logo_or_text_geometry_missing")
    if _decorative_detail_height_too_low(code, requirements):
        violations.append("decorative_detail_height_too_low")
    for check in requirements.quality_checks:
        if check.name == "hole_count_min":
            expected = int(check.params.get("count") or 0)
            if expected and _estimated_hole_marker_count(code) < expected:
                violations.append(f"hole_count_below_{expected}")
    return violations


def _violated_geometry_constraints(
    extents: tuple[float, float, float] | None,
    triangle_count: int | None,
    vertex_count: int | None,
    requirements: RequirementSpec,
) -> list[str]:
    violations: list[str] = []
    feature_set = set(requirements.required_features)
    has_geometry_intent = bool(feature_set & _GEOMETRY_FEATURES)

    if extents is not None:
        max_xy = max(extents[0], extents[1])
        min_extent = min(extents)
        if min_extent <= 0.0 or max_xy < _MIN_NONBLANK_SPAN_MM:
            violations.append("geometry_blank_or_degenerate")
        elif has_geometry_intent and extents[2] < _MIN_NONBLANK_Z_MM:
            violations.append("geometry_has_no_printable_z_height")
        if (
            feature_set & _DECORATIVE_FEATURES
            and extents[2] < _MIN_DECORATIVE_HEIGHT_MM
        ):
            violations.append("decorative_detail_height_too_low")

    if has_geometry_intent:
        if triangle_count is not None and triangle_count < _MIN_FEATURE_TRIANGLES:
            violations.append(f"triangle_count_below_{_MIN_FEATURE_TRIANGLES}")
        if vertex_count is not None and vertex_count < _MIN_FEATURE_VERTICES:
            violations.append(f"vertex_count_below_{_MIN_FEATURE_VERTICES}")

    for check in requirements.quality_checks:
        if check.name == "min_triangle_count" and triangle_count is not None:
            expected = int(check.params.get("count") or 0)
            if expected and triangle_count < expected:
                violation = f"triangle_count_below_{expected}"
                if violation not in violations:
                    violations.append(violation)
        if check.name == "min_vertex_count" and vertex_count is not None:
            expected = int(check.params.get("count") or 0)
            if expected and vertex_count < expected:
                violation = f"vertex_count_below_{expected}"
                if violation not in violations:
                    violations.append(violation)

    for dimension in requirements.dimensions:
        if dimension.name != "outer_span_xy":
            continue
        if extents is None:
            violations.append("outer_span_xy_unavailable")
            continue
        span_xy = max(extents[0], extents[1])
        tolerance = dimension.tolerance_mm if dimension.tolerance_mm is not None else 1.0
        if dimension.mode == "exact":
            if abs(span_xy - dimension.value_mm) > tolerance:
                violations.append(
                    f"outer_span_xy_out_of_tolerance_{dimension.value_mm:g}mm"
                )
        elif dimension.mode == "minimum":
            if span_xy + tolerance < dimension.value_mm:
                violations.append(f"outer_span_xy_below_{dimension.value_mm:g}mm")
        elif dimension.mode == "maximum" and span_xy - tolerance > dimension.value_mm:
            violations.append(f"outer_span_xy_above_{dimension.value_mm:g}mm")
    return violations


def _has_text_or_emboss_geometry(code: str) -> bool:
    return "text(" in code or "emboss" in code or "logo" in code


def _feature_has_geometry_evidence(code: str, markers: tuple[str, ...]) -> bool:
    return _OpenSCADModuleIndex.from_code(code).feature_has_geometry_evidence(markers)


def _module_bodies(code: str) -> dict[str, str]:
    bodies: dict[str, str] = {}
    pattern = re.compile(r"\bmodule\s+([a-z_][a-z0-9_]*)\s*\([^)]*\)\s*\{")
    for match in pattern.finditer(code):
        name = match.group(1)
        body_start = match.end()
        body_end = _matching_brace_index(code, body_start - 1)
        if body_end is None:
            continue
        bodies[name] = code[body_start:body_end]
    return bodies


def _matching_brace_index(code: str, opening_index: int) -> int | None:
    depth = 0
    for index in range(opening_index, len(code)):
        char = code[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def _name_matches_markers(name: str, markers: tuple[str, ...]) -> bool:
    for marker in markers:
        marker = marker.rstrip("(")
        if not marker or not re.fullmatch(r"[a-z0-9_]+", marker):
            continue
        if name == marker or name.startswith(f"{marker}_") or name.endswith(f"_{marker}"):
            return True
        if marker in {"bear_ears", "face_eyes"} and marker in name:
            return True
    return False


def _contains_geometry_primitive(body: str) -> bool:
    return (
        any(
            re.search(rf"\b{re.escape(primitive)}\s*\(", body)
            for primitive in _GEOMETRY_PRIMITIVES
            if primitive != "text("
        )
        or "text(" in body
    )


def _call_blocks(code: str, function_name: str) -> tuple[str, ...]:
    blocks: list[str] = []
    pattern = re.compile(rf"\b{re.escape(function_name)}\s*\([^)]*\)\s*\{{")
    for match in pattern.finditer(code):
        opening_index = match.end() - 1
        closing_index = _matching_brace_index(code, opening_index)
        if closing_index is not None:
            blocks.append(code[match.end() : closing_index])
    return tuple(blocks)


def _body_calls_module(body: str, module_name: str) -> bool:
    return re.search(rf"\b{re.escape(module_name)}\s*\(", body) is not None


def _block_has_generic_hole_cut(block: str) -> bool:
    return (
        re.search(r"\b(?:circle|cylinder)\s*\(", block) is not None
        and _contains_geometry_primitive(block)
    )


def _bracket_body_present(code: str) -> bool:
    if "bracket_body" in code:
        return _feature_has_geometry_evidence(code, ("bracket_body",))
    # Some local models generate an unnamed bracket as the positive solid in
    # difference() and then subtract cylinders. Accept that as geometry
    # evidence, but do not accept comments/strings because callers pass
    # _searchable_code().
    return re.search(r"\b(?:cube|polygon|linear_extrude)\s*\(", code) is not None


def _mounting_holes_present(code: str) -> bool:
    if any(marker in code for marker in _FEATURE_MARKERS["mounting_holes"]):
        return True
    return _estimated_difference_cylinder_cut_count(code) > 0


def _decorative_detail_height_too_low(
    code: str,
    requirements: RequirementSpec,
) -> bool:
    return _OpenSCADModuleIndex.from_code(code).decorative_feature_too_low(
        requirements
    )


def _linear_extrude_heights(code: str) -> list[float]:
    return [
        float(match.group(1))
        for match in re.finditer(
            r"\blinear_extrude\s*\(\s*(?:height\s*=\s*)?([0-9]+(?:\.[0-9]+)?)",
            code,
        )
    ]


def _estimated_hole_marker_count(code: str) -> int:
    generic_difference_cuts = _estimated_difference_cylinder_cut_count(code)
    if generic_difference_cuts:
        return generic_difference_cuts
    numbered = re.findall(r"(?:mounting_hole|screw_hole|hole)_\d+", code)
    if numbered:
        return len(set(numbered))
    helper_calls = re.findall(r"\b(?:mounting_hole|screw_hole|hole)\s*\(", code)
    if helper_calls:
        # Subtract one helper definition when the same helper is called multiple
        # times from main()/loops. Definitions and calls share the same syntax in
        # OpenSCAD, so this stays conservative for single-hole fixtures.
        definitions = re.findall(
            r"\bmodule\s+(?:mounting_hole|screw_hole|hole)\s*\(",
            code,
        )
        return max(1, len(helper_calls) - len(definitions))
    match = re.search(r"for\s*\([^)]*=\s*\[?0\s*:\s*(\d+)\s*-\s*1", code)
    if match:
        return int(match.group(1))
    if "mounting_holes" in code:
        count_match = re.search(r"(?:count|hole_count)\s*=\s*(\d+)", code)
        if count_match:
            return int(count_match.group(1))
    return 1 if "hole" in code else 0


def _estimated_difference_cylinder_cut_count(code: str) -> int:
    return sum(
        len(re.findall(r"\b(?:circle|cylinder)\s*\(", block))
        for block in _call_blocks(code, "difference")
    )


def _retry_hint(
    requirements: RequirementSpec,
    missing: list[str],
    violations: list[str],
) -> str:
    parts = []
    if missing:
        parts.append("Missing required features: " + ", ".join(missing) + ".")
    if violations:
        parts.append("Violated constraints: " + ", ".join(violations) + ".")
    if requirements.object_type:
        parts.append(
            f"Regenerate the {requirements.object_type} as complete OpenSCAD "
            "and model those requirements as real geometry, not prose."
        )
    else:
        parts.append(
            "Regenerate the OpenSCAD and model the missing requirements as "
            "real geometry, not prose."
        )
    return " ".join(parts)


def _strip_openscad_comments_and_strings(code: str) -> str:
    result: list[str] = []
    i = 0
    in_string = False
    while i < len(code):
        char = code[i]
        nxt = code[i + 1] if i + 1 < len(code) else ""

        if in_string:
            if char == "\\":
                i += 2
                continue
            if char == '"':
                in_string = False
            i += 1
            continue

        if char == '"':
            in_string = True
            i += 1
            continue
        if char == "/" and nxt == "/":
            i = code.find("\n", i)
            if i == -1:
                break
            result.append("\n")
            i += 1
            continue
        if char == "/" and nxt == "*":
            end = code.find("*/", i + 2)
            if end == -1:
                break
            result.append("\n" * code[i : end + 2].count("\n"))
            i = end + 2
            continue

        result.append(char)
        i += 1
    return "".join(result)


def _stl_stats(path: str | None) -> _STLStats | None:
    if path is None:
        return None
    stl_path = Path(path)
    if not stl_path.exists() or not stl_path.is_file():
        return None
    data = stl_path.read_bytes()
    vertices = _parse_ascii_vertices(data)
    triangle_count = len(vertices) // 3
    if not vertices:
        parsed_binary = _parse_binary_vertices(data)
        vertices = parsed_binary[0]
        triangle_count = parsed_binary[1]
    if not vertices:
        return None
    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    zs = [v[2] for v in vertices]
    extents = (
        max(xs) - min(xs),
        max(ys) - min(ys),
        max(zs) - min(zs),
    )
    return _STLStats(extents, triangle_count, len(vertices))


def _parse_ascii_vertices(data: bytes) -> list[tuple[float, float, float]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return []
    vertices: list[tuple[float, float, float]] = []
    for match in re.finditer(
        r"(?im)^\s*vertex\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)",
        text,
    ):
        vertices.append((float(match.group(1)), float(match.group(2)), float(match.group(3))))
    return vertices


def _parse_binary_vertices(data: bytes) -> tuple[list[tuple[float, float, float]], int]:
    if len(data) < 84:
        return ([], 0)
    triangle_count = struct.unpack_from("<I", data, 80)[0]
    expected_size = 84 + triangle_count * 50
    if len(data) < expected_size:
        return ([], 0)
    vertices: list[tuple[float, float, float]] = []
    offset = 84
    for _ in range(triangle_count):
        offset += 12  # normal
        for _vertex_index in range(3):
            vertices.append(struct.unpack_from("<fff", data, offset))
            offset += 12
        offset += 2  # attribute byte count
    return (vertices, triangle_count)
