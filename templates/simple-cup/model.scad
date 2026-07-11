// Model Forge deterministic simple cup fallback
// Source recipe: simple-cup v1.0.0 (ADR-0008)

$fn = 64;

outer_diameter_mm = {{outer_diameter_mm}};
height_mm = {{height_mm}};
wall_mm = {{wall_mm}};
inner_diameter_mm = outer_diameter_mm - (wall_mm * 2);

module cup_body() {
    difference() {
        cylinder(h = height_mm, d = outer_diameter_mm, center = false);
        translate([0, 0, wall_mm])
            cylinder(h = height_mm + 0.2, d = inner_diameter_mm, center = false);
    }
}

module main() {
    cup_body();
}

main();
