from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

Point2D = tuple[float, float]


@dataclass(frozen=True)
class VectorSilhouetteFixture:
    fixture_id: str
    request_id: str
    outline_points: tuple[Point2D, ...]
    loop_center: Point2D
    loop_outer_radius_mm: float
    loop_hole_radius_mm: float
    eye_centers: tuple[Point2D, Point2D]
    eye_radius_mm: float
    muzzle_center: Point2D
    muzzle_radius_mm: float
    nose_center: Point2D
    nose_radius_mm: float
    notes: str

    def __post_init__(self) -> None:
        _validate_fixture(self)


def _validate_fixture(fixture: VectorSilhouetteFixture) -> None:
    if len(fixture.outline_points) < 8:
        raise ValueError("Vector silhouette fixture requires at least 8 outline points.")
    if fixture.outline_points[0] == fixture.outline_points[-1]:
        raise ValueError("Vector silhouette outline must not repeat the first point.")
    for point in (
        *fixture.outline_points,
        fixture.loop_center,
        *fixture.eye_centers,
        fixture.muzzle_center,
        fixture.nose_center,
    ):
        _assert_finite_point(point)
    for radius_name, radius in (
        ("loop_outer_radius_mm", fixture.loop_outer_radius_mm),
        ("loop_hole_radius_mm", fixture.loop_hole_radius_mm),
        ("eye_radius_mm", fixture.eye_radius_mm),
        ("muzzle_radius_mm", fixture.muzzle_radius_mm),
        ("nose_radius_mm", fixture.nose_radius_mm),
    ):
        if not isfinite(radius) or radius <= 0:
            raise ValueError(f"{radius_name} must be a positive finite value.")
    if fixture.loop_hole_radius_mm >= fixture.loop_outer_radius_mm:
        raise ValueError("loop_hole_radius_mm must be smaller than loop_outer_radius_mm.")
    if _has_self_intersection(fixture.outline_points):
        raise ValueError("Vector silhouette outline must not be self-intersecting.")


def _assert_finite_point(point: Point2D) -> None:
    if len(point) != 2 or not all(isfinite(value) for value in point):
        raise ValueError("Vector silhouette points must be finite 2D coordinates.")


def _has_self_intersection(points: tuple[Point2D, ...]) -> bool:
    edge_count = len(points)
    edges = [
        (points[index], points[(index + 1) % edge_count])
        for index in range(edge_count)
    ]
    for first_index, first_edge in enumerate(edges):
        for second_index, second_edge in enumerate(edges):
            if second_index <= first_index:
                continue
            if _edges_are_adjacent(first_index, second_index, edge_count):
                continue
            if _segments_cross(*first_edge, *second_edge):
                return True
    return False


def _edges_are_adjacent(first: int, second: int, edge_count: int) -> bool:
    return abs(first - second) == 1 or {first, second} == {0, edge_count - 1}


def _segments_cross(
    a: Point2D,
    b: Point2D,
    c: Point2D,
    d: Point2D,
) -> bool:
    eps = 1e-9
    o1 = _orientation(a, b, c)
    o2 = _orientation(a, b, d)
    o3 = _orientation(c, d, a)
    o4 = _orientation(c, d, b)
    return o1 * o2 < -eps and o3 * o4 < -eps


def _orientation(a: Point2D, b: Point2D, c: Point2D) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _chaikin_smoothed_polygon(
    control_points: tuple[Point2D, ...],
    *,
    iterations: int,
) -> tuple[Point2D, ...]:
    if iterations < 1:
        return control_points

    points = list(control_points)
    for _ in range(iterations):
        smoothed: list[Point2D] = []
        for index, point in enumerate(points):
            next_point = points[(index + 1) % len(points)]
            smoothed.append(
                (
                    round(point[0] * 0.75 + next_point[0] * 0.25, 3),
                    round(point[1] * 0.75 + next_point[1] * 0.25, 3),
                )
            )
            smoothed.append(
                (
                    round(point[0] * 0.25 + next_point[0] * 0.75, 3),
                    round(point[1] * 0.25 + next_point[1] * 0.75, 3),
                )
            )
        points = smoothed
    return tuple(points)


_BEAR_OUTLINE_CONTROL_POINTS: tuple[Point2D, ...] = (
    (27.5, 47.6),
    (20.5, 46.9),
    (16.6, 44.6),
    (11.4, 45.6),
    (6.6, 41.8),
    (5.6, 36.9),
    (8.5, 33.2),
    (5.0, 28.4),
    (3.4, 21.2),
    (5.1, 14.5),
    (9.7, 8.3),
    (17.4, 4.2),
    (27.5, 2.9),
    (37.6, 4.2),
    (45.3, 8.3),
    (49.9, 14.5),
    (51.6, 21.2),
    (50.0, 28.4),
    (46.5, 33.2),
    (49.4, 36.9),
    (48.4, 41.8),
    (43.6, 45.6),
    (38.4, 44.6),
    (34.5, 46.9),
)


_RABBIT_OUTLINE_CONTROL_POINTS: tuple[Point2D, ...] = (
    (27.5, 47.4),
    (21.3, 58.4),
    (16.0, 61.8),
    (12.2, 57.4),
    (12.4, 49.4),
    (15.0, 39.4),
    (8.4, 32.8),
    (4.0, 24.0),
    (5.5, 14.3),
    (12.0, 7.0),
    (21.7, 3.4),
    (27.5, 3.0),
    (33.3, 3.4),
    (43.0, 7.0),
    (49.5, 14.3),
    (51.0, 24.0),
    (46.6, 32.8),
    (40.0, 39.4),
    (42.6, 49.4),
    (42.8, 57.4),
    (39.0, 61.8),
    (33.7, 58.4),
)


_BEAR_SILHOUETTE = VectorSilhouetteFixture(
    fixture_id="self_authored_bear_head_silhouette_v2",
    request_id="bear_keyring",
    outline_points=_chaikin_smoothed_polygon(
        _BEAR_OUTLINE_CONTROL_POINTS,
        iterations=2,
    ),
    loop_center=(27.5, 50.4),
    loop_outer_radius_mm=4.25,
    loop_hole_radius_mm=1.75,
    eye_centers=((21.7, 27.4), (33.3, 27.4)),
    eye_radius_mm=1.42,
    muzzle_center=(27.5, 19.6),
    muzzle_radius_mm=4.35,
    nose_center=(27.5, 22.1),
    nose_radius_mm=1.45,
    notes=(
        "Self-authored Chaikin-smoothed polygon: round bear head silhouette "
        "with ears in the outer outline and a smaller top loop."
    ),
)


_RABBIT_SILHOUETTE = VectorSilhouetteFixture(
    fixture_id="self_authored_rabbit_head_silhouette_v2",
    request_id="rabbit_keyring",
    outline_points=_chaikin_smoothed_polygon(
        _RABBIT_OUTLINE_CONTROL_POINTS,
        iterations=2,
    ),
    loop_center=(27.5, 51.0),
    loop_outer_radius_mm=3.75,
    loop_hole_radius_mm=1.55,
    eye_centers=((21.9, 27.1), (33.1, 27.1)),
    eye_radius_mm=1.32,
    muzzle_center=(27.5, 19.5),
    muzzle_radius_mm=3.9,
    nose_center=(27.5, 21.9),
    nose_radius_mm=1.25,
    notes=(
        "Self-authored Chaikin-smoothed polygon: rabbit head silhouette with "
        "long ears as the dominant outer contour and a smaller top loop."
    ),
)

_FIXTURES_BY_REQUEST_ID = {
    _BEAR_SILHOUETTE.request_id: _BEAR_SILHOUETTE,
    _RABBIT_SILHOUETTE.request_id: _RABBIT_SILHOUETTE,
}


def vector_silhouette_fixture_for_request_id(
    request_id: str,
) -> VectorSilhouetteFixture:
    try:
        return _FIXTURES_BY_REQUEST_ID[request_id]
    except KeyError as exc:
        raise ValueError(
            f"No vector silhouette fixture registered for request_id={request_id}"
        ) from exc


def all_vector_silhouette_fixtures() -> tuple[VectorSilhouetteFixture, ...]:
    return tuple(_FIXTURES_BY_REQUEST_ID[key] for key in sorted(_FIXTURES_BY_REQUEST_ID))


__all__ = [
    "Point2D",
    "VectorSilhouetteFixture",
    "all_vector_silhouette_fixtures",
    "vector_silhouette_fixture_for_request_id",
]
