from __future__ import annotations

from collections.abc import Iterable

from modules.newbie_request.catalog import load_component_catalog
from modules.newbie_request.l1_filter import FEATURE_ALIASES, L1StaticFilter
from modules.newbie_request.schemas import (
    AssemblyComponent,
    AssemblySpec,
    ComponentCatalog,
    CoordinateSpace,
    RenderedAssembly,
)


class AssemblyRenderError(ValueError):
    """Raised when an AssemblySpec cannot be rendered by the spike renderer."""


class AssemblyOpenSCADRenderer:
    """Minimal primitive-based renderer for Phase 12C assembly spikes."""

    renderer_name = "assembly_openscad_spike"
    renderer_version = "0.1.0"
    supported_component_types = frozenset(
        {
            "base_rounded_tag",
            "bear_head",
            "rabbit_head",
            "ears",
            "rabbit_ears",
            "eye_dots",
            "eyes",
            "nose_muzzle",
            "keyring_hole",
        }
    )

    def __init__(
        self,
        *,
        component_catalog: ComponentCatalog | None = None,
        l1_filter: L1StaticFilter | None = None,
    ) -> None:
        self._component_catalog = (
            component_catalog if component_catalog is not None else load_component_catalog()
        )
        self._components_by_type = self._component_catalog.by_type()
        self._l1_filter = (
            l1_filter
            if l1_filter is not None
            else L1StaticFilter(component_catalog=self._component_catalog)
        )

    def render(self, spec: AssemblySpec) -> RenderedAssembly:
        self._assert_renderable(spec)
        anchors = {anchor.id: anchor for anchor in spec.anchors}
        components_by_type = self._components_by_type_name(spec.components)
        base_h = spec.size_mm.height
        relief_h = max(1.0, min(1.6, base_h * 0.35))

        additive: list[str] = [
            f"    rounded_tag({spec.size_mm.width:.3f}, {spec.size_mm.depth:.3f}, "
            f"{base_h:.3f}, 5.000);"
        ]
        subtractive: list[str] = []

        if "bear_head" in components_by_type:
            center = anchors[components_by_type["bear_head"].anchor_id]
            r = min(spec.size_mm.width, spec.size_mm.depth) * 0.22
            additive.append(
                self._raised_circle("bear_head", center.x_mm, center.y_mm, r, base_h, relief_h)
            )
        if "rabbit_head" in components_by_type:
            center = anchors[components_by_type["rabbit_head"].anchor_id]
            r = min(spec.size_mm.width, spec.size_mm.depth) * 0.20
            additive.append(
                self._raised_circle("rabbit_head", center.x_mm, center.y_mm, r, base_h, relief_h)
            )

        if "ears" in components_by_type:
            top = anchors[components_by_type["ears"].anchor_id]
            additive.extend(self._bear_ears(top.x_mm, top.y_mm, base_h, relief_h))
        if "rabbit_ears" in components_by_type:
            top = anchors[components_by_type["rabbit_ears"].anchor_id]
            additive.extend(self._rabbit_ears(top.x_mm, top.y_mm, base_h, relief_h))

        if "eye_dots" in components_by_type or "eyes" in components_by_type:
            component = components_by_type.get("eye_dots") or components_by_type["eyes"]
            face = anchors[component.anchor_id]
            additive.extend(self._eyes(face.x_mm, face.y_mm, base_h, relief_h))

        if "nose_muzzle" in components_by_type:
            face = anchors[components_by_type["nose_muzzle"].anchor_id]
            additive.append(
                self._raised_circle(
                    "nose_or_muzzle",
                    face.x_mm,
                    face.y_mm - 4.2,
                    3.0,
                    base_h,
                    relief_h,
                )
            )

        if "keyring_hole" in components_by_type:
            top = anchors[components_by_type["keyring_hole"].anchor_id]
            subtractive.append(
                f"    translate([{top.x_mm:.3f}, {top.y_mm:.3f}, -0.500]) "
                f"cylinder(h={base_h + relief_h + 1.000:.3f}, r=3.200);"
            )

        code = self._source(additive, subtractive)
        return RenderedAssembly(
            source_catalog_id=spec.source_catalog_id,
            object_type=spec.object_type,
            category=spec.category,
            source_code=code,
            renderer_name=self.renderer_name,
            renderer_version=self.renderer_version,
            metadata={
                "route": "component_assembly",
                "visual_quality_status": spec.quality_policy.visual_quality_status.value,
                "visual_quality_required": spec.quality_policy.visual_quality_required,
                "component_types": tuple(component.component_type for component in spec.components),
                "stl_smoke_required_for_phase": "12C optional",
                "gcode_phase": "12D",
            },
        )

    def _assert_renderable(self, spec: AssemblySpec) -> None:
        if spec.coordinate_space is not CoordinateSpace.LOCAL_BOUNDS_0_WIDTH:
            raise AssemblyRenderError(
                "AssemblyOpenSCADRenderer only supports LOCAL_BOUNDS_0_WIDTH"
            )
        report = self._l1_filter.validate(spec)
        if not report.passed:
            raise AssemblyRenderError(
                "AssemblySpec failed L1 static filter: "
                + ", ".join(report.codes)
            )
        component_types = tuple(component.component_type for component in spec.components)
        duplicate_component_types = tuple(
            sorted(
                component_type
                for component_type in set(component_types)
                if component_types.count(component_type) > 1
            )
        )
        if duplicate_component_types:
            raise AssemblyRenderError(
                "AssemblyOpenSCADRenderer does not support duplicate component types: "
                + ", ".join(duplicate_component_types)
            )

        unsupported_component_types = tuple(
            sorted(set(component_types) - self.supported_component_types)
        )
        if unsupported_component_types:
            raise AssemblyRenderError(
                "AssemblyOpenSCADRenderer does not support component types: "
                + ", ".join(unsupported_component_types)
            )

        if "base_rounded_tag" not in component_types:
            raise AssemblyRenderError(
                "AssemblyOpenSCADRenderer requires an explicit base_rounded_tag component"
            )

        evidence = self._feature_evidence(spec.components)
        missing = tuple(
            feature
            for feature in spec.required_features
            if evidence.isdisjoint(FEATURE_ALIASES.get(feature, frozenset({feature})))
        )
        if missing:
            raise AssemblyRenderError(
                "AssemblySpec is missing renderable features: "
                + ", ".join(missing)
            )

    def _feature_evidence(self, components: Iterable[AssemblyComponent]) -> frozenset[str]:
        evidence: set[str] = set()
        for component in components:
            capability = self._components_by_type.get(component.component_type)
            if capability is not None:
                evidence.update(capability.provides_features)
        return frozenset(evidence)

    @staticmethod
    def _components_by_type_name(
        components: Iterable[AssemblyComponent],
    ) -> dict[str, AssemblyComponent]:
        return {component.component_type: component for component in components}

    @staticmethod
    def _raised_circle(
        label: str,
        x: float,
        y: float,
        radius: float,
        base_h: float,
        relief_h: float,
    ) -> str:
        return (
            f"    // {label}\n"
            f"    translate([{x:.3f}, {y:.3f}, {base_h - 0.050:.3f}]) "
            f"cylinder(h={relief_h:.3f}, r={radius:.3f});"
        )

    @staticmethod
    def _bear_ears(x: float, y: float, base_h: float, relief_h: float) -> list[str]:
        return [
            AssemblyOpenSCADRenderer._raised_circle(
                "left_ear", x - 11.0, y - 4.0, 6.2, base_h, relief_h
            ),
            AssemblyOpenSCADRenderer._raised_circle(
                "right_ear", x + 11.0, y - 4.0, 6.2, base_h, relief_h
            ),
        ]

    @staticmethod
    def _rabbit_ears(x: float, y: float, base_h: float, relief_h: float) -> list[str]:
        return [
            (
                f"    // left_rabbit_ear\n"
                f"    translate([{x - 7.0:.3f}, {y - 7.0:.3f}, {base_h - 0.050:.3f}]) "
                f"scale([0.600, 1.650, 1.000]) cylinder(h={relief_h:.3f}, r=4.800);"
            ),
            (
                f"    // right_rabbit_ear\n"
                f"    translate([{x + 7.0:.3f}, {y - 7.0:.3f}, {base_h - 0.050:.3f}]) "
                f"scale([0.600, 1.650, 1.000]) cylinder(h={relief_h:.3f}, r=4.800);"
            ),
        ]

    @staticmethod
    def _eyes(x: float, y: float, base_h: float, relief_h: float) -> list[str]:
        eye_h = max(0.8, relief_h * 0.75)
        return [
            AssemblyOpenSCADRenderer._raised_circle(
                "left_eye", x - 5.2, y + 2.4, 1.35, base_h + relief_h, eye_h
            ),
            AssemblyOpenSCADRenderer._raised_circle(
                "right_eye", x + 5.2, y + 2.4, 1.35, base_h + relief_h, eye_h
            ),
        ]

    @staticmethod
    def _source(additive: list[str], subtractive: list[str]) -> str:
        additive_block = "\n".join(additive)
        subtractive_block = "\n".join(subtractive)
        if not subtractive_block:
            subtractive_block = "    // no subtractive components"
        return "\n".join(
            [
                "$fn = 48;",
                "",
                "module rounded_tag(w, d, h, r) {",
                "  linear_extrude(height=h)",
                "    hull() {",
                "      translate([r, r]) circle(r=r);",
                "      translate([w-r, r]) circle(r=r);",
                "      translate([r, d-r]) circle(r=r);",
                "      translate([w-r, d-r]) circle(r=r);",
                "    }",
                "}",
                "",
                "module main() {",
                "  difference() {",
                "    union() {",
                additive_block,
                "    }",
                subtractive_block,
                "  }",
                "}",
                "",
                "main();",
            ]
        )


__all__ = ["AssemblyOpenSCADRenderer", "AssemblyRenderError"]
