from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class NewbieRoute(StrEnum):
    ASK_USER = "ask_user"
    COMPONENT_ASSEMBLY = "component_assembly"
    CURATED_ASSET = "curated_asset"
    DRAFT_ASSET = "draft_asset"
    MANUAL_REVIEW = "manual_review"
    OPENSCAD_FREEFORM_EXPERIMENTAL = "openscad_freeform_experimental"
    OPENSCAD_TEMPLATE = "openscad_template"
    PARAMETRIC_TEMPLATE = "parametric_template"
    TEXT_TEMPLATE = "text_template"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CoordinateSpace(StrEnum):
    LOCAL_BOUNDS_0_WIDTH = "local_bounds_0_width"
    CENTER_ORIGIN_MM = "center_origin_mm"


class VisualQualityStatus(StrEnum):
    NOT_EVALUATED = "not_evaluated"
    REVIEW_REQUIRED = "review_required"
    MANUAL_PASS_CANDIDATE = "manual_pass_candidate"
    MANUAL_PASS = "manual_pass"
    MANUAL_FAIL = "manual_fail"
    AUTOMATED_PASS = "automated_pass"
    AUTOMATED_FAIL = "automated_fail"


class SizeMM(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    width: float = Field(gt=0)
    depth: float = Field(gt=0)
    height: float = Field(gt=0)


class NewbieCatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_]*$")
    user_prompt_ko: str = Field(min_length=1)
    object_type: str = Field(min_length=1)
    category: str = Field(min_length=1)
    target_user_intent: str = Field(min_length=1)
    default_route: NewbieRoute
    fallback_route: NewbieRoute
    required_features: tuple[str, ...] = Field(min_length=1)
    min_size_mm: SizeMM
    visual_quality_required: bool
    manufacturing_risk: RiskLevel
    ip_risk: RiskLevel
    notes: str = ""


class NewbieRequestCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entries: tuple[NewbieCatalogEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _request_ids_are_unique(self) -> NewbieRequestCatalog:
        request_ids = [entry.request_id for entry in self.entries]
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("Duplicate request_id in newbie request catalog")
        return self

    def by_id(self) -> dict[str, NewbieCatalogEntry]:
        return {entry.request_id: entry for entry in self.entries}


class Anchor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    role: str = Field(default="generic", min_length=1)
    x_mm: float
    y_mm: float
    z_mm: float = 0.0


class AssemblyComponent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    component_type: str = Field(min_length=1)
    anchor_id: str = Field(min_length=1)
    required: bool = True
    scale: float = Field(default=1.0, gt=0)
    features: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)


class ComponentCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component_type: str = Field(min_length=1)
    provides_features: tuple[str, ...] = Field(min_length=1)
    allowed_categories: tuple[str, ...] = ()
    allowed_object_types: tuple[str, ...] = ()
    allowed_anchor_roles: tuple[str, ...] = ()
    min_size_mm: SizeMM | None = None
    min_scale: float = Field(default=0.1, gt=0)
    manufacturing_risk: RiskLevel = RiskLevel.LOW
    visual_quality_role: str = Field(default="none", min_length=1)
    renderer_hint: str = Field(default="component_assembly", min_length=1)
    license_status: str = Field(default="internal_placeholder", min_length=1)
    provenance_status: str = Field(default="not_external_asset", min_length=1)


class ComponentCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    components: tuple[ComponentCapability, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _component_types_are_unique(self) -> ComponentCatalog:
        component_types = [component.component_type for component in self.components]
        if len(component_types) != len(set(component_types)):
            raise ValueError("Duplicate component_type in component catalog")
        return self

    def by_type(self) -> dict[str, ComponentCapability]:
        return {component.component_type: component for component in self.components}


class ManufacturingConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    single_connected_part: bool = True
    min_wall_mm: float = Field(default=2.5, gt=0)
    beginner_printable: bool = True
    max_overhang_deg: float | None = Field(default=None, ge=0, le=90)


class QualityPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    visual_quality_required: bool = False
    manual_review_required: bool = False
    visual_quality_status: VisualQualityStatus = VisualQualityStatus.NOT_EVALUATED


class AssemblySpec(BaseModel):
    """Typed intermediate representation for beginner-friendly object assembly.

    ``LOCAL_BOUNDS_0_WIDTH`` anchors use assembly-local coordinates:
    ``0 <= x <= width``, ``0 <= y <= depth``, and ``0 <= z <= height``.
    Center-origin CAD renderers must add an explicit transform layer instead of
    reinterpreting this IR in-place.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    object_type: str = Field(min_length=1)
    category: str = Field(min_length=1)
    base_shape: str = Field(min_length=1)
    source_catalog_id: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9_]*$",
    )
    source_prompt_terms: tuple[str, ...] = ()
    coordinate_space: CoordinateSpace = CoordinateSpace.LOCAL_BOUNDS_0_WIDTH
    size_mm: SizeMM
    components: tuple[AssemblyComponent, ...] = Field(min_length=1)
    anchors: tuple[Anchor, ...] = Field(min_length=1)
    required_features: tuple[str, ...] = ()
    manufacturing_constraints: ManufacturingConstraints = Field(
        default_factory=ManufacturingConstraints
    )
    quality_policy: QualityPolicy = Field(default_factory=QualityPolicy)
    ip_keywords: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _references_are_consistent(self) -> AssemblySpec:
        anchor_ids = [anchor.id for anchor in self.anchors]
        if len(anchor_ids) != len(set(anchor_ids)):
            raise ValueError("Duplicate anchor id in AssemblySpec")

        component_ids = [component.id for component in self.components]
        if len(component_ids) != len(set(component_ids)):
            raise ValueError("Duplicate component id in AssemblySpec")

        anchor_id_set = set(anchor_ids)
        missing_anchor_refs = [
            component.anchor_id
            for component in self.components
            if component.anchor_id not in anchor_id_set
        ]
        if missing_anchor_refs:
            raise ValueError(
                "AssemblyComponent references unknown anchor id: "
                f"{', '.join(sorted(set(missing_anchor_refs)))}"
            )
        return self


class L1Violation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    path: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class L1FilterReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    violations: tuple[L1Violation, ...] = ()

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(violation.code for violation in self.violations)


class RouteSelectionReason(StrEnum):
    EXACT_REQUEST_ID = "exact_request_id"
    EXACT_USER_PROMPT = "exact_user_prompt"
    CATEGORY_FALLBACK = "category_fallback"
    UNKNOWN_REQUEST = "unknown_request"


class RouteSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str | None = None
    user_prompt_ko: str | None = None
    category: str | None = None
    object_type: str | None = None


class RouteSelectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    selected_route: NewbieRoute
    fallback_route: NewbieRoute
    reason: RouteSelectionReason
    request_id: str | None = None
    category: str | None = None
    object_type: str | None = None
    required_features: tuple[str, ...] = ()
    visual_quality_required: bool = False


class RenderedAssembly(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_catalog_id: str | None = None
    object_type: str
    category: str
    dialect: str = "openscad"
    source_code: str = Field(min_length=1)
    renderer_name: str = Field(min_length=1)
    renderer_version: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class L2GeometryReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    stl_exists: bool
    stl_size_bytes: int = Field(ge=0)
    extents_mm: tuple[float, float, float] | None = None
    volume_mm3: float | None = Field(default=None, ge=0)
    watertight: bool | None = None
    triangle_count: int | None = Field(default=None, ge=0)
    vertex_count: int | None = Field(default=None, ge=0)
    connected_component_count: int | None = Field(default=None, ge=0)
    keyring_hole_candidate: bool | None = None
    raised_detail_height_mm: float | None = Field(default=None, ge=0)
    visual_quality_status: VisualQualityStatus = VisualQualityStatus.NOT_EVALUATED
    metadata: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "Anchor",
    "AssemblyComponent",
    "AssemblySpec",
    "ComponentCapability",
    "ComponentCatalog",
    "CoordinateSpace",
    "L1FilterReport",
    "L1Violation",
    "L2GeometryReport",
    "ManufacturingConstraints",
    "NewbieCatalogEntry",
    "NewbieRequestCatalog",
    "NewbieRoute",
    "QualityPolicy",
    "RenderedAssembly",
    "RiskLevel",
    "RouteSelectionReason",
    "RouteSelectionRequest",
    "RouteSelectionResult",
    "SizeMM",
    "VisualQualityStatus",
]
