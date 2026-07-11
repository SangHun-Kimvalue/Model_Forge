// Model Forge parametric pen holder
// Rendered by RecipeRenderer — do not edit values directly.
// Source recipe: pen-holder v1.0.0 (ADR-0008)
//
// Parameters injected:
//   diameter_mm  — inner diameter (mm)
//   height_mm    — total height (mm)
//   wall_mm      — wall thickness (mm)

$fn = 64;

inner_dia_mm = {{diameter_mm}};
height_mm    = {{height_mm}};
wall_mm      = {{wall_mm}};

outer_r = (inner_dia_mm / 2) + wall_mm;
inner_r = inner_dia_mm / 2;

difference() {
    // Outer shell
    cylinder(h = height_mm, r = outer_r, center = false);
    // Inner cavity (open top, closed bottom via wall_mm offset)
    translate([0, 0, wall_mm])
        cylinder(h = height_mm, r = inner_r, center = false);
}
