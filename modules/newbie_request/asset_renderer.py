from __future__ import annotations

from modules.newbie_request.asset_catalog import DecorativeAssetEntry
from modules.newbie_request.schemas import RenderedAssembly


class DecorativeAssetRenderError(ValueError):
    """Raised when a decorative catalog asset cannot be rendered."""


class DecorativeAssetKeyringRenderer:
    """Deterministic renderer for self-authored decorative keyring assets."""

    renderer_name = "decorative_asset_keyring_mvp"
    renderer_version = "0.1.0"
    supported_category = "keyring"

    def render(self, asset: DecorativeAssetEntry) -> RenderedAssembly:
        if asset.category != self.supported_category:
            raise DecorativeAssetRenderError(
                "DecorativeAssetKeyringRenderer only supports keyring assets"
            )
        source = self._source(asset)
        return RenderedAssembly(
            source_catalog_id=asset.asset_id,
            object_type=f"{asset.subject}_keyring",
            category=asset.category,
            source_code=source,
            renderer_name=self.renderer_name,
            renderer_version=self.renderer_version,
            metadata={
                "route": "curated_asset",
                "asset_id": asset.asset_id,
                "subject": asset.subject,
                "style": asset.style,
                "source_type": asset.source_type.value,
                "license": asset.license,
                "source_url_or_owner": asset.source_url_or_owner,
                "legal_review_status": asset.legal_review_status.value,
                "commercial_allowed": asset.commercial_allowed,
                "redistribution_allowed": asset.redistribution_allowed,
                "modification_allowed": asset.modification_allowed,
                "provenance_note": asset.provenance_note,
                "renderer_strategy": "self_authored_vector_primitives",
                "external_asset": False,
                "visual_quality_status": asset.visual_quality_status.value,
                "visual_quality_required": asset.visual_quality_required,
                "printability_status": asset.printability_status.value,
            },
        )

    def _source(self, asset: DecorativeAssetEntry) -> str:
        if asset.subject == "bear":
            return _bear_source(asset)
        if asset.subject == "rabbit":
            return _rabbit_source(asset)
        raise DecorativeAssetRenderError(
            f"Unsupported decorative asset subject: {asset.subject}"
        )


def _common_modules() -> list[str]:
    return [
        "$fn = 128;",
        "",
        "module ellipse_2d(x, y, rx, ry) {",
        "  translate([x, y]) scale([rx, ry]) circle(r=1);",
        "}",
        "",
        "module capsule_2d(x1, y1, x2, y2, r) {",
        "  hull() {",
        "    translate([x1, y1]) circle(r=r);",
        "    translate([x2, y2]) circle(r=r);",
        "  }",
        "}",
        "",
        "module extruded_2d(h) {",
        "  linear_extrude(height=h) children();",
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
        "module raised_capsule(x1, y1, x2, y2, r, z, h) {",
        "  translate([0, 0, z])",
        "    linear_extrude(height=h)",
        "      capsule_2d(x1, y1, x2, y2, r);",
        "}",
        "",
    ]


def _raised_disc_line(x: float, y: float, r: float, z: float, h: float) -> str:
    return f"    raised_disc({x:.3f}, {y:.3f}, {r:.3f}, {z:.3f}, {h:.3f});"


def _raised_ellipse_line(
    x: float,
    y: float,
    rx: float,
    ry: float,
    z: float,
    h: float,
) -> str:
    return (
        f"    raised_ellipse({x:.3f}, {y:.3f}, {rx:.3f}, {ry:.3f}, "
        f"{z:.3f}, {h:.3f});"
    )


def _raised_capsule_line(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    r: float,
    z: float,
    h: float,
) -> str:
    return (
        f"    raised_capsule({x1:.3f}, {y1:.3f}, {x2:.3f}, {y2:.3f}, "
        f"{r:.3f}, {z:.3f}, {h:.3f});"
    )


def _bear_source(asset: DecorativeAssetEntry) -> str:
    base_h = asset.recommended_thickness_mm
    relief_h = 1.35
    sleepy = asset.style == "sleepy_face"
    eye_lines = [
        _raised_capsule_line(22.5, 29.5, 25.0, 29.9, 0.52, base_h + 0.95, 0.7),
        _raised_capsule_line(33.0, 29.9, 35.5, 29.5, 0.52, base_h + 0.95, 0.7),
    ] if sleepy else [
        _raised_disc_line(23.6, 29.2, 1.65, base_h - 0.05, relief_h),
        _raised_disc_line(34.4, 29.2, 1.65, base_h - 0.05, relief_h),
        _raised_disc_line(24.1, 29.9, 0.42, base_h + relief_h, 0.48),
        _raised_disc_line(34.9, 29.9, 0.42, base_h + relief_h, 0.48),
    ]
    return "\n".join(
        [
            *_common_modules(),
            "module asset_body_2d() {",
            "  union() {",
            "    ellipse_2d(29.000, 27.000, 22.800, 21.400);",
            "    ellipse_2d(13.200, 42.500, 7.800, 7.300);",
            "    ellipse_2d(44.800, 42.500, 7.800, 7.300);",
            "    capsule_2d(29.000, 45.400, 29.000, 52.500, 4.000);",
            "  }",
            "}",
            "",
            "module face_relief() {",
            "  // inner ears",
            _raised_ellipse_line(13.2, 42.5, 4.0, 3.35, base_h - 0.04, 0.9),
            _raised_ellipse_line(44.8, 42.5, 4.0, 3.35, base_h - 0.04, 0.9),
            "  // eyes",
            *eye_lines,
            "  // muzzle, nose, cheeks and mouth",
            _raised_ellipse_line(29.0, 20.8, 6.0, 4.2, base_h - 0.05, relief_h),
            _raised_ellipse_line(29.0, 23.2, 2.1, 1.45, base_h + 1.0, 0.85),
            _raised_capsule_line(29.0, 21.9, 29.0, 18.7, 0.45, base_h + 1.05, 0.65),
            _raised_disc_line(18.5, 22.5, 1.15, base_h - 0.02, 0.85),
            _raised_disc_line(39.5, 22.5, 1.15, base_h - 0.02, 0.85),
            "}",
            "",
            _main_module(base_h=base_h, relief_h=relief_h, hole_x=29.0, hole_y=52.6),
        ]
    )


def _rabbit_source(asset: DecorativeAssetEntry) -> str:
    base_h = asset.recommended_thickness_mm
    relief_h = 1.35
    round_cheeks = asset.style == "round_cheeks"
    cheek_radius = 1.7 if round_cheeks else 1.1
    return "\n".join(
        [
            *_common_modules(),
            "module asset_body_2d() {",
            "  union() {",
            "    ellipse_2d(29.000, 25.500, 22.000, 20.500);",
            "    capsule_2d(20.300, 39.000, 16.500, 61.500, 5.200);",
            "    capsule_2d(37.700, 39.000, 41.500, 61.500, 5.200);",
            "    capsule_2d(29.000, 48.000, 29.000, 53.800, 3.700);",
            "  }",
            "}",
            "",
            "module face_relief() {",
            "  // inner ears",
            _raised_capsule_line(20.3, 41.5, 17.3, 58.8, 2.0, base_h - 0.04, 0.95),
            _raised_capsule_line(37.7, 41.5, 40.7, 58.8, 2.0, base_h - 0.04, 0.95),
            "  // eyes and highlights",
            _raised_disc_line(23.6, 28.2, 1.5, base_h - 0.05, relief_h),
            _raised_disc_line(34.4, 28.2, 1.5, base_h - 0.05, relief_h),
            _raised_disc_line(24.0, 28.85, 0.38, base_h + relief_h, 0.48),
            _raised_disc_line(34.8, 28.85, 0.38, base_h + relief_h, 0.48),
            "  // muzzle, nose, cheeks and mouth",
            _raised_ellipse_line(29.0, 20.3, 5.2, 3.8, base_h - 0.05, relief_h),
            _raised_ellipse_line(29.0, 22.5, 1.7, 1.2, base_h + 1.0, 0.85),
            _raised_disc_line(18.5, 21.9, cheek_radius, base_h - 0.02, 0.85),
            _raised_disc_line(39.5, 21.9, cheek_radius, base_h - 0.02, 0.85),
            _raised_capsule_line(29.0, 21.3, 29.0, 18.5, 0.39, base_h + 1.05, 0.65),
            "}",
            "",
            _main_module(base_h=base_h, relief_h=relief_h, hole_x=29.0, hole_y=53.8),
        ]
    )


def _main_module(*, base_h: float, relief_h: float, hole_x: float, hole_y: float) -> str:
    return "\n".join(
        [
            "module main() {",
            "  difference() {",
            "    union() {",
            "      extruded_2d("
            f"{base_h:.3f}) asset_body_2d();",
            "      face_relief();",
            "    }",
            f"    translate([{hole_x:.3f}, {hole_y:.3f}, -0.500]) "
            f"cylinder(h={base_h + relief_h + 1.000:.3f}, r=1.750);",
            "  }",
            "}",
            "",
            "main();",
        ]
    )


__all__ = ["DecorativeAssetKeyringRenderer", "DecorativeAssetRenderError"]
