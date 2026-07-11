from __future__ import annotations

from collections.abc import Iterable

from modules.newbie_request.catalog import (
    load_component_catalog,
    load_newbie_request_catalog,
)
from modules.newbie_request.schemas import (
    AssemblyComponent,
    AssemblySpec,
    ComponentCatalog,
    CoordinateSpace,
    L1FilterReport,
    L1Violation,
    NewbieCatalogEntry,
    NewbieRequestCatalog,
)

DEFAULT_RISKY_IP_KEYWORDS = frozenset(
    {
        "disney",
        "hello kitty",
        "lego",
        "marvel",
        "mickey",
        "minion",
        "nintendo",
        "pikachu",
        "pokemon",
        "star wars",
        "디즈니",
        "레고",
        "마블",
        "미키",
        "스타워즈",
        "포켓몬",
        "피카츄",
        "헬로키티",
    }
)

FEATURE_ALIASES = {
    "bear_head": frozenset({"bear_head"}),
    "rabbit_head": frozenset({"rabbit_head"}),
    "cat_head": frozenset({"cat_head"}),
    "ears": frozenset({"ears", "rabbit_ears", "cat_ears"}),
    "eyes": frozenset({"eyes", "eye_dots"}),
    "nose_or_muzzle": frozenset({"nose_or_muzzle", "nose_muzzle"}),
    "keyring_hole": frozenset({"keyring_hole"}),
    "mounting_holes": frozenset({"mounting_holes", "mounting_hole"}),
}


class L1StaticFilter:
    """Cheap Assembly IR validation that runs before CAD/mesh runtimes."""

    def __init__(
        self,
        *,
        component_catalog: ComponentCatalog | None = None,
        request_catalog: NewbieRequestCatalog | None = None,
        risky_ip_keywords: Iterable[str] = DEFAULT_RISKY_IP_KEYWORDS,
        min_wall_mm: float = 2.5,
    ) -> None:
        catalog = component_catalog if component_catalog is not None else load_component_catalog()
        self._components_by_type = catalog.by_type()
        newbie_catalog = (
            request_catalog if request_catalog is not None else load_newbie_request_catalog()
        )
        self._request_catalog_by_id = newbie_catalog.by_id()
        self._risky_ip_keywords = frozenset(
            keyword.casefold() for keyword in risky_ip_keywords
        )
        self._min_wall_mm = min_wall_mm

    def validate(self, spec: AssemblySpec) -> L1FilterReport:
        violations: list[L1Violation] = []
        catalog_entry = self._catalog_entry(spec)
        violations.extend(self._catalog_policy_violations(spec, catalog_entry))
        violations.extend(self._unknown_component_violations(spec))
        violations.extend(self._component_scope_violations(spec))
        violations.extend(self._unsupported_component_feature_violations(spec))
        violations.extend(self._size_violations(spec))
        violations.extend(self._anchor_violations(spec))
        violations.extend(self._required_feature_violations(spec))
        violations.extend(self._ip_violations(spec))

        return L1FilterReport(passed=not violations, violations=tuple(violations))

    def _catalog_entry(self, spec: AssemblySpec) -> NewbieCatalogEntry | None:
        if spec.source_catalog_id is None:
            return None
        return self._request_catalog_by_id.get(spec.source_catalog_id)

    def _catalog_policy_violations(
        self,
        spec: AssemblySpec,
        catalog_entry: NewbieCatalogEntry | None,
    ) -> list[L1Violation]:
        if spec.source_catalog_id is None:
            return []
        if catalog_entry is None:
            return [
                L1Violation(
                    code="source_catalog_unknown",
                    message="AssemblySpec references an unknown catalog request id",
                    path="source_catalog_id",
                    metadata={"source_catalog_id": spec.source_catalog_id},
                )
            ]

        violations: list[L1Violation] = []
        if spec.object_type != catalog_entry.object_type:
            violations.append(
                L1Violation(
                    code="catalog_object_type_mismatch",
                    message="AssemblySpec object_type does not match catalog policy",
                    path="object_type",
                    metadata={
                        "expected": catalog_entry.object_type,
                        "actual": spec.object_type,
                    },
                )
            )
        if spec.category != catalog_entry.category:
            violations.append(
                L1Violation(
                    code="catalog_category_mismatch",
                    message="AssemblySpec category does not match catalog policy",
                    path="category",
                    metadata={
                        "expected": catalog_entry.category,
                        "actual": spec.category,
                    },
                )
            )

        min_size = catalog_entry.min_size_mm
        if (
            spec.size_mm.width < min_size.width
            or spec.size_mm.depth < min_size.depth
            or spec.size_mm.height < min_size.height
        ):
            violations.append(
                L1Violation(
                    code="catalog_min_size_violation",
                    message="AssemblySpec size is below the matched catalog minimum",
                    path="size_mm",
                    metadata={
                        "source_catalog_id": catalog_entry.request_id,
                        "minimum_width_mm": min_size.width,
                        "minimum_depth_mm": min_size.depth,
                        "minimum_height_mm": min_size.height,
                        "actual_width_mm": spec.size_mm.width,
                        "actual_depth_mm": spec.size_mm.depth,
                        "actual_height_mm": spec.size_mm.height,
                    },
                )
            )

        missing_required = tuple(
            feature
            for feature in catalog_entry.required_features
            if feature not in spec.required_features
        )
        if missing_required:
            violations.append(
                L1Violation(
                    code="catalog_required_feature_missing",
                    message="AssemblySpec omits features required by catalog policy",
                    path="required_features",
                    metadata={
                        "source_catalog_id": catalog_entry.request_id,
                        "features": missing_required,
                    },
                )
            )

        if (
            catalog_entry.visual_quality_required
            and not spec.quality_policy.visual_quality_required
        ):
            violations.append(
                L1Violation(
                    code="catalog_visual_quality_policy_missing",
                    message="AssemblySpec omits required visual quality policy",
                    path="quality_policy.visual_quality_required",
                    metadata={"source_catalog_id": catalog_entry.request_id},
                )
            )
        return violations

    def _unknown_component_violations(self, spec: AssemblySpec) -> list[L1Violation]:
        violations: list[L1Violation] = []
        for index, component in enumerate(spec.components):
            if component.component_type not in self._components_by_type:
                violations.append(
                    L1Violation(
                        code="unknown_component",
                        message=f"Unknown component: {component.component_type}",
                        path=f"components[{index}].component_type",
                        metadata={"component_id": component.id},
                    )
                )
        return violations

    def _component_scope_violations(self, spec: AssemblySpec) -> list[L1Violation]:
        violations: list[L1Violation] = []
        anchors_by_id = {anchor.id: anchor for anchor in spec.anchors}

        for index, component in enumerate(spec.components):
            capability = self._components_by_type.get(component.component_type)
            if capability is None:
                continue

            if (
                capability.allowed_categories
                and spec.category not in capability.allowed_categories
            ):
                violations.append(
                    L1Violation(
                        code="component_category_not_allowed",
                        message=(
                            f"Component '{component.component_type}' is not allowed "
                            f"for category '{spec.category}'"
                        ),
                        path=f"components[{index}].component_type",
                        metadata={
                            "component_id": component.id,
                            "category": spec.category,
                        },
                    )
                )

            if (
                capability.allowed_object_types
                and spec.object_type not in capability.allowed_object_types
            ):
                violations.append(
                    L1Violation(
                        code="component_object_type_not_allowed",
                        message=(
                            f"Component '{component.component_type}' is not allowed "
                            f"for object_type '{spec.object_type}'"
                        ),
                        path=f"components[{index}].component_type",
                        metadata={
                            "component_id": component.id,
                            "object_type": spec.object_type,
                        },
                    )
                )

            anchor = anchors_by_id.get(component.anchor_id)
            if (
                anchor is not None
                and capability.allowed_anchor_roles
                and anchor.role not in capability.allowed_anchor_roles
            ):
                violations.append(
                    L1Violation(
                        code="component_anchor_role_not_allowed",
                        message=(
                            f"Component '{component.component_type}' is not allowed "
                            f"on anchor role '{anchor.role}'"
                        ),
                        path=f"components[{index}].anchor_id",
                        metadata={
                            "component_id": component.id,
                            "anchor_id": anchor.id,
                            "anchor_role": anchor.role,
                        },
                    )
                )

            if component.scale < capability.min_scale:
                violations.append(
                    L1Violation(
                        code="component_scale_too_small",
                        message=(
                            f"Component '{component.component_type}' scale is below "
                            "its capability minimum"
                        ),
                        path=f"components[{index}].scale",
                        metadata={
                            "component_id": component.id,
                            "minimum_scale": capability.min_scale,
                            "actual_scale": component.scale,
                        },
                    )
                )

        return violations

    def _unsupported_component_feature_violations(
        self, spec: AssemblySpec
    ) -> list[L1Violation]:
        violations: list[L1Violation] = []
        for index, component in enumerate(spec.components):
            capability = self._components_by_type.get(component.component_type)
            if capability is None:
                continue
            unsupported = tuple(
                sorted(set(component.features) - set(capability.provides_features))
            )
            if unsupported:
                violations.append(
                    L1Violation(
                        code="unsupported_component_feature",
                        message=(
                            "AssemblyComponent declares features not provided by "
                            "its component capability"
                        ),
                        path=f"components[{index}].features",
                        metadata={
                            "component_id": component.id,
                            "component_type": component.component_type,
                            "features": unsupported,
                        },
                    )
                )
        return violations

    def _size_violations(self, spec: AssemblySpec) -> list[L1Violation]:
        violations: list[L1Violation] = []
        if spec.manufacturing_constraints.min_wall_mm < self._min_wall_mm:
            violations.append(
                L1Violation(
                    code="min_wall_thickness_violation",
                    message=(
                        "Minimum wall thickness is below the product L1 threshold"
                    ),
                    path="manufacturing_constraints.min_wall_mm",
                    metadata={
                        "minimum_mm": self._min_wall_mm,
                        "actual_mm": spec.manufacturing_constraints.min_wall_mm,
                    },
                )
            )

        for index, component in enumerate(spec.components):
            capability = self._components_by_type.get(component.component_type)
            if capability is None or capability.min_size_mm is None:
                continue
            min_size = capability.min_size_mm
            if (
                spec.size_mm.width < min_size.width
                or spec.size_mm.depth < min_size.depth
                or spec.size_mm.height < min_size.height
            ):
                violations.append(
                    L1Violation(
                        code="component_min_size_violation",
                        message=(
                            "AssemblySpec size is below a component capability minimum"
                        ),
                        path="size_mm",
                        metadata={
                            "component_index": index,
                            "component_type": component.component_type,
                            "minimum_width_mm": min_size.width,
                            "minimum_depth_mm": min_size.depth,
                            "minimum_height_mm": min_size.height,
                        },
                    )
                )

        if self._is_keyring(spec) and (
            spec.size_mm.width < 35
            or spec.size_mm.depth < 25
            or spec.size_mm.height < self._min_wall_mm
        ):
            violations.append(
                L1Violation(
                    code="size_too_small",
                    message="Keyring assembly is too small for beginner-safe output",
                    path="size_mm",
                    metadata={
                        "minimum_width_mm": 35,
                        "minimum_depth_mm": 25,
                        "minimum_height_mm": self._min_wall_mm,
                    },
                )
            )
        return violations

    def _anchor_violations(self, spec: AssemblySpec) -> list[L1Violation]:
        violations: list[L1Violation] = []
        if spec.coordinate_space is not CoordinateSpace.LOCAL_BOUNDS_0_WIDTH:
            return [
                L1Violation(
                    code="unsupported_coordinate_space",
                    message=(
                        "Phase 12 Assembly IR anchors must use local 0..width/depth "
                        "bounds coordinates"
                    ),
                    path="coordinate_space",
                    metadata={"coordinate_space": spec.coordinate_space.value},
                )
            ]
        for index, anchor in enumerate(spec.anchors):
            if not (0 <= anchor.x_mm <= spec.size_mm.width):
                violations.append(
                    L1Violation(
                        code="anchor_out_of_bounds",
                        message=f"Anchor '{anchor.id}' x is outside the base bounds",
                        path=f"anchors[{index}].x_mm",
                        metadata={
                            "anchor_id": anchor.id,
                            "min_mm": 0,
                            "max_mm": spec.size_mm.width,
                            "actual_mm": anchor.x_mm,
                        },
                    )
                )
            if not (0 <= anchor.y_mm <= spec.size_mm.depth):
                violations.append(
                    L1Violation(
                        code="anchor_out_of_bounds",
                        message=f"Anchor '{anchor.id}' y is outside the base bounds",
                        path=f"anchors[{index}].y_mm",
                        metadata={
                            "anchor_id": anchor.id,
                            "min_mm": 0,
                            "max_mm": spec.size_mm.depth,
                            "actual_mm": anchor.y_mm,
                        },
                    )
                )
            if not (0 <= anchor.z_mm <= spec.size_mm.height):
                violations.append(
                    L1Violation(
                        code="anchor_out_of_bounds",
                        message=f"Anchor '{anchor.id}' z is outside the base bounds",
                        path=f"anchors[{index}].z_mm",
                        metadata={
                            "anchor_id": anchor.id,
                            "min_mm": 0,
                            "max_mm": spec.size_mm.height,
                            "actual_mm": anchor.z_mm,
                        },
                    )
                )
        return violations

    def _required_feature_violations(self, spec: AssemblySpec) -> list[L1Violation]:
        evidence = self._feature_evidence_from_catalog(spec.components)
        violations: list[L1Violation] = []

        for feature in spec.required_features:
            accepted_tokens = FEATURE_ALIASES.get(feature, frozenset({feature}))
            if evidence.isdisjoint(accepted_tokens):
                violations.append(
                    L1Violation(
                        code="required_feature_missing",
                        message=f"Required feature is missing: {feature}",
                        path="required_features",
                        metadata={"feature": feature},
                    )
                )

        if self._is_keyring(spec) and "keyring_hole" not in evidence:
            violations.append(
                L1Violation(
                    code="keyring_hole_missing",
                    message="Keyring route requires a keyring_hole capability",
                    path="components",
                    metadata={"object_type": spec.object_type},
                )
            )
        return violations

    def _ip_violations(self, spec: AssemblySpec) -> list[L1Violation]:
        haystack = " ".join(
            [
                spec.object_type,
                spec.category,
                spec.base_shape,
                *spec.source_prompt_terms,
                *spec.required_features,
                *spec.ip_keywords,
                *(
                    token
                    for component in spec.components
                    for token in self._component_search_tokens(component)
                ),
            ]
        ).casefold()
        hits = tuple(
            sorted(keyword for keyword in self._risky_ip_keywords if keyword in haystack)
        )
        if not hits:
            return []
        return [
            L1Violation(
                code="risky_ip_keyword",
                message="Assembly request contains a risky IP or trademark keyword",
                path="ip_keywords",
                metadata={"keywords": hits},
            )
        ]

    def _feature_evidence_from_catalog(
        self, components: tuple[AssemblyComponent, ...]
    ) -> frozenset[str]:
        tokens: set[str] = set()
        for component in components:
            capability = self._components_by_type.get(component.component_type)
            if capability is not None:
                tokens.update(capability.provides_features)
        return frozenset(tokens)

    @staticmethod
    def _component_search_tokens(component: AssemblyComponent) -> tuple[str, ...]:
        return (
            component.id,
            component.component_type,
            *component.features,
            *tuple(str(value) for value in component.metadata.values()),
        )

    @staticmethod
    def _is_keyring(spec: AssemblySpec) -> bool:
        return (
            "keyring" in spec.object_type
            or "keyring" in spec.category
            or "keyring_hole" in spec.required_features
        )


__all__ = [
    "DEFAULT_RISKY_IP_KEYWORDS",
    "L1StaticFilter",
]
