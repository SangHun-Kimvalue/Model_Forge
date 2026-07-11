from __future__ import annotations

from collections.abc import Iterable

from modules.newbie_request.catalog import load_component_catalog
from modules.newbie_request.l1_filter import L1StaticFilter
from modules.newbie_request.schemas import (
    AssemblySpec,
    ComponentCatalog,
    CoordinateSpace,
    RenderedAssembly,
    VisualQualityStatus,
)
from modules.newbie_request.silhouette_fixtures import (
    Point2D,
    VectorSilhouetteFixture,
    vector_silhouette_fixture_for_request_id,
)


class VectorSilhouetteRenderError(ValueError):
    """Raised when a keyring AssemblySpec cannot use the silhouette renderer."""


class VectorSilhouetteKeyringRenderer:
    """Self-authored vector silhouette renderer for decorative keyring spikes."""

    renderer_name = "vector_silhouette_keyring_spike"
    renderer_version = "0.1.1"
    supported_category = "decorative_keyring"

    def __init__(
        self,
        *,
        component_catalog: ComponentCatalog | None = None,
        l1_filter: L1StaticFilter | None = None,
    ) -> None:
        self._component_catalog = (
            component_catalog if component_catalog is not None else load_component_catalog()
        )
        self._l1_filter = (
            l1_filter
            if l1_filter is not None
            else L1StaticFilter(component_catalog=self._component_catalog)
        )

    def render(self, spec: AssemblySpec) -> RenderedAssembly:
        self._assert_renderable(spec)
        fixture = vector_silhouette_fixture_for_request_id(spec.source_catalog_id or "")
        relief_h = max(1.0, min(1.6, spec.size_mm.height * 0.35))
        source = self._source(spec, fixture, relief_h)
        return RenderedAssembly(
            source_catalog_id=spec.source_catalog_id,
            object_type=spec.object_type,
            category=spec.category,
            source_code=source,
            renderer_name=self.renderer_name,
            renderer_version=self.renderer_version,
            metadata={
                "route": "component_assembly",
                "strategy": "vector_silhouette",
                "silhouette_fixture_id": fixture.fixture_id,
                "silhouette_point_count": len(fixture.outline_points),
                "silhouette_curve_method": "chaikin_smoothed_polygon",
                "visual_polish_phase": "12G.1",
                "visual_quality_required": spec.quality_policy.visual_quality_required,
                "visual_quality_status": VisualQualityStatus.REVIEW_REQUIRED.value
                if spec.quality_policy.visual_quality_required
                else VisualQualityStatus.NOT_EVALUATED.value,
                "keyring_hole_strategy": "small_top_loop",
                "external_asset": False,
            },
        )

    def _assert_renderable(self, spec: AssemblySpec) -> None:
        if spec.coordinate_space is not CoordinateSpace.LOCAL_BOUNDS_0_WIDTH:
            raise VectorSilhouetteRenderError(
                "VectorSilhouetteKeyringRenderer only supports LOCAL_BOUNDS_0_WIDTH"
            )
        if spec.category != self.supported_category:
            raise VectorSilhouetteRenderError(
                "VectorSilhouetteKeyringRenderer only supports decorative_keyring"
            )
        report = self._l1_filter.validate(spec)
        if not report.passed:
            raise VectorSilhouetteRenderError(
                "AssemblySpec failed L1 static filter: "
                + ", ".join(report.codes)
            )
        try:
            vector_silhouette_fixture_for_request_id(spec.source_catalog_id or "")
        except ValueError as exc:
            raise VectorSilhouetteRenderError(str(exc)) from exc

    def _source(
        self,
        spec: AssemblySpec,
        fixture: VectorSilhouetteFixture,
        relief_h: float,
    ) -> str:
        base_h = spec.size_mm.height
        points = _format_points(fixture.outline_points)
        loop_x, loop_y = fixture.loop_center
        left_eye, right_eye = fixture.eye_centers
        muzzle_x, muzzle_y = fixture.muzzle_center
        nose_x, nose_y = fixture.nose_center
        return "\n".join(
            [
                "$fn = 96;",
                "",
                f"silhouette_points = {points};",
                "",
                "module silhouette_body(h) {",
                "  linear_extrude(height=h)",
                "    polygon(points=silhouette_points);",
                "}",
                "",
                "module raised_disc(x, y, r, z, h) {",
                "  translate([x, y, z]) cylinder(h=h, r=r);",
                "}",
                "",
                "module raised_ellipse(x, y, rx, ry, z, h) {",
                "  translate([x, y, z]) scale([rx, ry, 1]) cylinder(h=h, r=1);",
                "}",
                "",
                "module loop_tab() {",
                f"  translate([{loop_x:.3f}, {loop_y:.3f}, 0]) "
                f"cylinder(h={base_h:.3f}, r={fixture.loop_outer_radius_mm:.3f});",
                "}",
                "",
                "module face_relief() {",
                f"  raised_disc({left_eye[0]:.3f}, {left_eye[1]:.3f}, "
                f"{fixture.eye_radius_mm:.3f}, {base_h - 0.050:.3f}, {relief_h:.3f});",
                f"  raised_disc({right_eye[0]:.3f}, {right_eye[1]:.3f}, "
                f"{fixture.eye_radius_mm:.3f}, {base_h - 0.050:.3f}, {relief_h:.3f});",
                f"  raised_ellipse({muzzle_x:.3f}, {muzzle_y:.3f}, "
                f"{fixture.muzzle_radius_mm:.3f}, {fixture.muzzle_radius_mm * 0.720:.3f}, "
                f"{base_h - 0.050:.3f}, {relief_h:.3f});",
                f"  raised_disc({nose_x:.3f}, {nose_y:.3f}, "
                f"{fixture.nose_radius_mm:.3f}, {base_h + relief_h - 0.100:.3f}, "
                f"{max(0.7, relief_h * 0.6):.3f});",
                "}",
                "",
                "module main() {",
                "  difference() {",
                "    union() {",
                f"      // {fixture.fixture_id}",
                "      silhouette_body("
                f"{base_h:.3f});",
                "      loop_tab();",
                "      face_relief();",
                "    }",
                f"    translate([{loop_x:.3f}, {loop_y:.3f}, -0.500]) "
                f"cylinder(h={base_h + relief_h + 1.000:.3f}, "
                f"r={fixture.loop_hole_radius_mm:.3f});",
                "  }",
                "}",
                "",
                "main();",
            ]
        )


def _format_points(points: Iterable[Point2D]) -> str:
    return "[" + ", ".join(f"[{x:.3f}, {y:.3f}]" for x, y in points) + "]"


__all__ = ["VectorSilhouetteKeyringRenderer", "VectorSilhouetteRenderError"]
