"""Render validated manufacturing parameters into deterministic OpenSCAD."""

from __future__ import annotations

import re

from modules.parametric_cad.schemas import ManufacturingParams

_SAFE_TEXT_RE = re.compile(r"[^A-Za-z0-9 _.-]+")


def render_openscad(params: ManufacturingParams) -> str:
    """Return a self-contained OpenSCAD script with ``module main()``."""

    if params.part_type == "drone_frame":
        body = _drone_frame(params)
    elif params.part_type == "cup":
        body = _cup(params)
    elif params.part_type == "bracket":
        body = _bracket(params)
    elif params.part_type == "pen_holder":
        body = _pen_holder(params)
    elif params.part_type == "fixture":
        body = _fixture(params)
    else:  # pragma: no cover - pydantic Literal prevents this.
        raise ValueError(f"unsupported part_type: {params.part_type}")
    return "\n".join([_header(params), body.rstrip(), "", "main();", ""])


def _header(params: ManufacturingParams) -> str:
    return "\n".join(
        [
            "// Model Forge parametric OpenSCAD template",
            f"// part_type: {params.part_type}",
            f"// notes: {_comment(params.notes)}",
            "$fn = 48;",
            "",
        ]
    )


def _drone_frame(params: ManufacturingParams) -> str:
    x, y = params.outer_size_mm
    thickness = params.thickness_mm
    motor_r = params.motor_mount_diameter_mm / 2
    hub_r = max(18.0, min(x, y) * 0.12)
    arm_r = max(5.0, min(x, y) * 0.035)
    motor_hole_r = max(1.5, min(motor_r * 0.18, 3.2))
    mount_x = x / 2 - motor_r
    mount_y = y / 2 - motor_r
    logo = _logo(params, z=thickness, max_xy=min(x, y))
    return f"""module body2d() {{
    union() {{
        circle(r={_n(hub_r)});
        for (sx = [-1, 1]) for (sy = [-1, 1]) {{
            hull() {{
                circle(r={_n(arm_r)});
                translate([sx * {_n(mount_x)}, sy * {_n(mount_y)}])
                    circle(r={_n(motor_r)});
            }}
        }}
    }}
}}

module main() {{
    union() {{
        difference() {{
            linear_extrude(height={_n(thickness)}) body2d();
            for (sx = [-1, 1]) for (sy = [-1, 1]) {{
                translate([sx * {_n(mount_x)}, sy * {_n(mount_y)}, -0.5])
                    cylinder(h={_n(thickness + 1)}, r={_n(motor_hole_r)});
            }}
        }}
{_indent(logo, 8)}
    }}
}}"""


def _cup(params: ManufacturingParams) -> str:
    x, y = params.outer_size_mm
    radius = min(x, y) / 2
    height = params.height_mm
    wall = params.wall_thickness_mm
    bottom = max(wall, params.thickness_mm)
    logo = _logo(params, z=height * 0.55, max_xy=min(x, y), rotate_to_front=True)
    return f"""module cup_shell() {{
    difference() {{
        cylinder(h={_n(height)}, r={_n(radius)});
        translate([0, 0, {_n(bottom)}])
            cylinder(h={_n(height + 1)}, r={_n(radius - wall)});
    }}
}}

module main() {{
    union() {{
        cup_shell();
{_indent(logo, 8)}
    }}
}}"""


def _bracket(params: ManufacturingParams) -> str:
    x, y = params.outer_size_mm
    thickness = params.thickness_mm
    height = params.height_mm
    hole_r = params.hole_diameter_mm / 2
    logo = _logo(params, z=height + 0.05, max_xy=min(x, y))
    return f"""module main() {{
    union() {{
        difference() {{
            union() {{
                translate([-{_n(x / 2)}, -{_n(y / 2)}, 0])
                    cube([{_n(x)}, {_n(y)}, {_n(thickness)}]);
                translate([-{_n(x / 2)}, {_n(y / 2 - thickness)}, 0])
                    cube([{_n(x)}, {_n(thickness)}, {_n(height)}]);
            }}
            for (px = [-{_n(x * 0.25)}, {_n(x * 0.25)}]) {{
                translate([px, {_n(y / 2 - thickness / 2)}, {_n(height * 0.55)}])
                    rotate([90, 0, 0])
                    cylinder(h={_n(thickness + 2)}, r={_n(hole_r)}, center=true);
            }}
        }}
{_indent(logo, 8)}
    }}
}}"""


def _pen_holder(params: ManufacturingParams) -> str:
    x, y = params.outer_size_mm
    height = params.height_mm
    hole_count = max(1, min(params.hole_count, 4))
    hole_r = max(params.hole_diameter_mm / 2, 5.0)
    spacing = min(x, y) / (hole_count + 1)
    return f"""module main() {{
    difference() {{
        translate([-{_n(x / 2)}, -{_n(y / 2)}, 0])
            cube([{_n(x)}, {_n(y)}, {_n(height)}]);
        for (i = [1:{hole_count}]) {{
            translate([-{_n(x / 2)} + i * {_n(spacing)}, 0, {_n(params.thickness_mm)}])
                cylinder(h={_n(height + 1)}, r={_n(hole_r)});
        }}
    }}
}}"""


def _fixture(params: ManufacturingParams) -> str:
    x, y = params.outer_size_mm
    thickness = params.thickness_mm
    center_r = params.hole_diameter_mm / 2
    corner_r = max(1.5, min(params.hole_diameter_mm / 4, 3.0))
    corner_x = max(0.0, x / 2 - corner_r * 3)
    corner_y = max(0.0, y / 2 - corner_r * 3)
    return f"""module main() {{
    difference() {{
        translate([-{_n(x / 2)}, -{_n(y / 2)}, 0])
            cube([{_n(x)}, {_n(y)}, {_n(thickness)}]);
        translate([0, 0, -0.5])
            cylinder(h={_n(thickness + 1)}, r={_n(center_r)});
        for (sx = [-1, 1]) for (sy = [-1, 1]) {{
            translate([sx * {_n(corner_x)}, sy * {_n(corner_y)}, -0.5])
                cylinder(h={_n(thickness + 1)}, r={_n(corner_r)});
        }}
    }}
}}"""


def _logo(
    params: ManufacturingParams,
    *,
    z: float,
    max_xy: float,
    rotate_to_front: bool = False,
) -> str:
    if params.logo.mode != "embossed" or not params.logo.text:
        return "// no logo"
    text = _safe_text(params.logo.text)
    size = max(5.0, min(max_xy * 0.12, 18.0))
    extrusion = params.logo.height_mm
    base = (
        f'linear_extrude(height={_n(extrusion)}) '
        f'text("{text}", size={_n(size)}, halign="center", valign="center");'
    )
    if rotate_to_front:
        return (
            f"translate([0, -{_n(max_xy / 2 + 0.05)}, {_n(z)}])\n"
            f"    rotate([90, 0, 0]) {base}"
        )
    return f"translate([0, 0, {_n(z)}]) {base}"


def _safe_text(value: str) -> str:
    return _SAFE_TEXT_RE.sub("", value).replace('"', "")[:24] or "Generic Printer"


def _comment(value: str) -> str:
    return value.replace("\n", " ").replace("\r", " ")[:160]


def _indent(text: str, spaces: int) -> str:
    prefix = " " * spaces
    return "\n".join(prefix + line if line else line for line in text.splitlines())


def _n(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")
