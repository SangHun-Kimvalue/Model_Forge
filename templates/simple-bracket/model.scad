// Model Forge deterministic simple bracket fallback
// Source recipe: simple-bracket v1.0.0 (ADR-0008)

$fn = 48;

length_mm = {{length_mm}};
width_mm = {{width_mm}};
thickness_mm = {{thickness_mm}};
hole_diameter_mm = {{hole_diameter_mm}};
hole_count = {{hole_count}};
edge_offset_mm = 12;

module bracket_body() {
    cube([length_mm, width_mm, thickness_mm], center = false);
}

module mounting_hole_1() {
    translate([edge_offset_mm, edge_offset_mm, -0.1])
        cylinder(h = thickness_mm + 0.2, d = hole_diameter_mm, center = false);
}

module mounting_hole_2() {
    translate([length_mm - edge_offset_mm, edge_offset_mm, -0.1])
        cylinder(h = thickness_mm + 0.2, d = hole_diameter_mm, center = false);
}

module mounting_hole_3() {
    translate([edge_offset_mm, width_mm - edge_offset_mm, -0.1])
        cylinder(h = thickness_mm + 0.2, d = hole_diameter_mm, center = false);
}

module mounting_hole_4() {
    translate([length_mm - edge_offset_mm, width_mm - edge_offset_mm, -0.1])
        cylinder(h = thickness_mm + 0.2, d = hole_diameter_mm, center = false);
}

module main() {
    difference() {
        bracket_body();
        mounting_hole_1();
        mounting_hole_2();
        if (hole_count >= 3) {
            mounting_hole_3();
        }
        if (hole_count >= 4) {
            mounting_hole_4();
        }
    }
}

main();
