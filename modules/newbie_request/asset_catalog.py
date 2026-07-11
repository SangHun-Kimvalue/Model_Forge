from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from modules.newbie_request.catalog import load_newbie_request_catalog
from modules.newbie_request.printability import (
    PrintabilityReport,
    printability_gcode_metadata,
    printability_manifest_metadata,
)
from modules.newbie_request.route_selector import NewbieRouteSelector
from modules.newbie_request.schemas import (
    NewbieRoute,
    RenderedAssembly,
    RouteSelectionRequest,
    SizeMM,
    VisualQualityStatus,
)
from modules.newbie_request.visual_quality import (
    is_visual_quality_product_ready,
    visual_quality_metadata,
)

DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_DECORATIVE_ASSET_CATALOG_PATH = DATA_DIR / "decorative_asset_catalog.json"
DEFAULT_SUBJECT_CATEGORY_LABELS_PATH = DATA_DIR / "subject_category_labels.json"


class AssetCandidateExplanationSource(StrEnum):
    RUNTIME_CATALOG = "runtime_catalog"
    DRAFT_QUEUE = "draft_queue"
    REFERENCE_CANDIDATE = "reference_candidate"
    NEW_DRAFT = "new_draft"
    ASK_USER = "ask_user"


class AssetCandidateMatchLevel(StrEnum):
    EXACT = "exact"
    ALIAS_EXACT = "alias_exact"
    STYLE_MISSING = "style_missing"
    STYLE_MISMATCH = "style_mismatch"
    BLOCKED = "blocked"
    NOT_APPLICABLE = "not_applicable"
    NO_MATCH = "no_match"


class AssetCandidateExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(min_length=1)
    asset_id: str | None = None
    source: AssetCandidateExplanationSource
    match_level: AssetCandidateMatchLevel
    subject: str | None = None
    category: str | None = None
    style: str | None = None
    reason_code: str = Field(min_length=1)
    display_text_ko: str = Field(min_length=1)


class SubjectCategoryLabelRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1)
    label_ko: str = Field(min_length=1)


class SubjectCategoryLabelTable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(min_length=1)
    subjects: tuple[SubjectCategoryLabelRow, ...] = ()
    categories: tuple[SubjectCategoryLabelRow, ...] = ()

    @model_validator(mode="after")
    def _keys_are_unique(self) -> SubjectCategoryLabelTable:
        _ensure_unique_label_keys(self.subjects, "subject")
        _ensure_unique_label_keys(self.categories, "category")
        return self

    def subject_label(self, key: str) -> str:
        return _label_for(self.subjects, key, label_type="subject")

    def category_label(self, key: str) -> str:
        return _label_for(self.categories, key, label_type="category")


class DecorativeAssetSourceType(StrEnum):
    SELF_AUTHORED = "self_authored"
    LICENSE_CLEARED = "license_cleared"


class DecorativeAssetPrintabilityStatus(StrEnum):
    REVIEW_REQUIRED = "review_required"
    SMOKE_PASSED = "smoke_passed"
    NOT_EVALUATED = "not_evaluated"


class DecorativeAssetLegalReviewStatus(StrEnum):
    NOT_REVIEWED = "not_reviewed"
    APPROVED = "approved"
    REJECTED = "rejected"


class DecorativeAssetEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_]*$")
    request_ids: tuple[str, ...] = Field(min_length=1)
    subject: str = Field(min_length=1)
    category: str = Field(min_length=1)
    style: str = Field(min_length=1)
    source_type: DecorativeAssetSourceType
    license: str = Field(min_length=1)
    source_url_or_owner: str = Field(min_length=1)
    legal_review_status: DecorativeAssetLegalReviewStatus
    commercial_allowed: bool
    redistribution_allowed: bool
    modification_allowed: bool
    provenance_note: str = Field(min_length=1)
    default_size_mm: SizeMM
    min_size_mm: SizeMM
    recommended_thickness_mm: float = Field(gt=0)
    printability_status: DecorativeAssetPrintabilityStatus
    visual_quality_required: bool
    visual_quality_status: VisualQualityStatus
    required_features: tuple[str, ...] = Field(min_length=1)
    reviewer: str | None = None
    reviewed_at: str | None = None
    review_note: str | None = None

    @model_validator(mode="after")
    def _decorative_assets_require_visual_gate(self) -> DecorativeAssetEntry:
        if not self.visual_quality_required:
            raise ValueError(
                "visual_quality_required must be true for decorative assets"
            )
        return self


class DecorativeAssetCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    assets: tuple[DecorativeAssetEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _asset_ids_are_unique(self) -> DecorativeAssetCatalog:
        asset_ids = [asset.asset_id for asset in self.assets]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("Duplicate asset_id in decorative asset catalog")
        return self

    def by_id(self) -> dict[str, DecorativeAssetEntry]:
        return {asset.asset_id: asset for asset in self.assets}


class DecorativeAssetSelectionStatus(StrEnum):
    SELECTED = "selected"
    ASK_USER = "ask_user"
    REVIEW_REQUIRED = "review_required"


class DecorativeAssetSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str | None = None
    user_prompt_ko: str | None = None
    subject: str | None = None
    category: str | None = None
    style: str | None = None


class DecorativeAssetSelectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: DecorativeAssetSelectionStatus
    selected_route: NewbieRoute
    asset_id: str | None = None
    request_id: str | None = None
    subject: str | None = None
    category: str | None = None
    style: str | None = None
    reason: str
    candidate_asset_ids: tuple[str, ...] = ()
    candidate_explanations: tuple[AssetCandidateExplanation, ...] = ()


class DecorativeAssetSelector:
    """Deterministic selector for license/provenance-reviewed decorative assets."""

    def __init__(
        self,
        catalog: DecorativeAssetCatalog,
        *,
        label_table: SubjectCategoryLabelTable | None = None,
    ) -> None:
        self._catalog = catalog
        self._label_table = label_table or load_subject_category_label_table()
        _validate_catalog_labels(catalog, self._label_table)

    def select(
        self,
        request: DecorativeAssetSelectionRequest,
    ) -> DecorativeAssetSelectionResult:
        request_id = request.request_id
        if request_id is None and request.user_prompt_ko is not None:
            route_result = NewbieRouteSelector(load_newbie_request_catalog()).select(
                RouteSelectionRequest(user_prompt_ko=request.user_prompt_ko)
            )
            request_id = route_result.request_id

        candidates = tuple(
            asset
            for asset in self._catalog.assets
            if _matches_request(asset, request_id=request_id, subject=request.subject)
            and _matches_category(asset, request.category)
            and _matches_style(asset, request.style)
        )
        if not candidates:
            return DecorativeAssetSelectionResult(
                status=DecorativeAssetSelectionStatus.ASK_USER,
                selected_route=NewbieRoute.ASK_USER,
                request_id=request_id,
                subject=request.subject,
                category=request.category,
                style=request.style,
                reason="no_matching_decorative_asset",
                candidate_explanations=(
                    _no_match_explanation(
                        subject=request.subject,
                        category=request.category,
                        style=request.style,
                    ),
                ),
            )

        selected = candidates[0]
        return DecorativeAssetSelectionResult(
            status=DecorativeAssetSelectionStatus.SELECTED,
            selected_route=NewbieRoute.CURATED_ASSET,
            asset_id=selected.asset_id,
            request_id=request_id,
            subject=selected.subject,
            category=selected.category,
            style=selected.style,
            reason="catalog_asset_selected",
            candidate_asset_ids=tuple(asset.asset_id for asset in candidates),
            candidate_explanations=_runtime_catalog_explanations(
                candidates,
                selected_asset_id=selected.asset_id,
                label_table=self._label_table,
            ),
        )


def load_decorative_asset_catalog(
    path: str | Path | None = None,
) -> DecorativeAssetCatalog:
    catalog_path = Path(path) if path is not None else DEFAULT_DECORATIVE_ASSET_CATALOG_PATH
    with catalog_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return DecorativeAssetCatalog.model_validate(payload)


def load_subject_category_label_table(
    path: str | Path | None = None,
) -> SubjectCategoryLabelTable:
    label_path = (
        Path(path) if path is not None else DEFAULT_SUBJECT_CATEGORY_LABELS_PATH
    )
    with label_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return SubjectCategoryLabelTable.model_validate(payload)


def decorative_asset_manifest_metadata(
    *,
    asset: DecorativeAssetEntry,
    rendered: RenderedAssembly,
    l2_report: object,
    printability_report: PrintabilityReport | None = None,
    selection: DecorativeAssetSelectionResult | None = None,
    gcode_stage: str = "not_run",
    gcode_reason: str = "phase_12i_orca_not_run",
) -> dict[str, object]:
    return {
        "source_catalog_id": asset.asset_id,
        "route": NewbieRoute.CURATED_ASSET.value,
        "asset": asset.model_dump(mode="json"),
        "asset_selection": selection.model_dump(mode="json")
        if selection is not None
        else None,
        "renderer": rendered.renderer_name,
        "renderer_version": rendered.renderer_version,
        "renderer_metadata": dict(rendered.metadata),
        "legal_review_status": asset.legal_review_status.value,
        "release_allowed": _release_allowed(asset, printability_report),
        "l2_geometry": l2_report.model_dump(mode="json")
        if hasattr(l2_report, "model_dump")
        else l2_report,
        "printability": printability_manifest_metadata(printability_report),
        **visual_quality_metadata(
            required=asset.visual_quality_required,
            required_features=asset.required_features,
            status=asset.visual_quality_status,
        ),
        "gcode": {
            **printability_gcode_metadata(
                printability_report,
                requested_stage=gcode_stage,
                requested_reason=gcode_reason,
            )
        },
    }


def _matches_request(
    asset: DecorativeAssetEntry,
    *,
    request_id: str | None,
    subject: str | None,
) -> bool:
    if request_id is not None and request_id not in asset.request_ids:
        return False
    if subject is not None and asset.subject != subject:
        return False
    return request_id is not None or subject is not None


def _matches_style(asset: DecorativeAssetEntry, style: str | None) -> bool:
    return style is None or asset.style == style


def _matches_category(asset: DecorativeAssetEntry, category: str | None) -> bool:
    return category is None or asset.category == category


def _release_allowed(
    asset: DecorativeAssetEntry,
    printability_report: PrintabilityReport | None,
) -> bool:
    visual_metadata = visual_quality_metadata(
        required=asset.visual_quality_required,
        required_features=asset.required_features,
        status=asset.visual_quality_status,
    )
    return (
        asset.legal_review_status is DecorativeAssetLegalReviewStatus.APPROVED
        and asset.printability_status is DecorativeAssetPrintabilityStatus.SMOKE_PASSED
        and printability_report is not None
        and printability_report.can_slice
        and asset.commercial_allowed
        and asset.redistribution_allowed
        and asset.modification_allowed
        and is_visual_quality_product_ready(visual_metadata)
    )


def _runtime_catalog_explanations(
    candidates: tuple[DecorativeAssetEntry, ...],
    *,
    selected_asset_id: str,
    label_table: SubjectCategoryLabelTable,
) -> tuple[AssetCandidateExplanation, ...]:
    explanations: list[AssetCandidateExplanation] = []
    for asset in candidates:
        selected = asset.asset_id == selected_asset_id
        subject_label = label_table.subject_label(asset.subject)
        category_label = label_table.category_label(asset.category)
        explanations.append(
            AssetCandidateExplanation(
                candidate_id=f"runtime_catalog:{asset.asset_id}",
                asset_id=asset.asset_id,
                source=AssetCandidateExplanationSource.RUNTIME_CATALOG,
                match_level=AssetCandidateMatchLevel.EXACT,
                subject=asset.subject,
                category=asset.category,
                style=asset.style,
                reason_code=(
                    "catalog_asset_selected"
                    if selected
                    else "additional_exact_catalog_candidate"
                ),
                display_text_ko=(
                    "요청한 "
                    f"{subject_label} {_with_josa_and(category_label)} "
                    "일치하는 검증 후보입니다."
                    if selected
                    else (
                        "같은 요청에 사용할 수 있는 추가 "
                        f"{subject_label} {category_label} 후보입니다."
                    )
                ),
            )
        )
    return tuple(explanations)


def _validate_catalog_labels(
    catalog: DecorativeAssetCatalog,
    label_table: SubjectCategoryLabelTable,
) -> None:
    for asset in catalog.assets:
        label_table.subject_label(asset.subject)
        label_table.category_label(asset.category)


def _no_match_explanation(
    *,
    subject: str | None,
    category: str | None,
    style: str | None,
) -> AssetCandidateExplanation:
    return AssetCandidateExplanation(
        candidate_id="ask_user:no_matching_decorative_asset",
        asset_id=None,
        source=AssetCandidateExplanationSource.ASK_USER,
        match_level=AssetCandidateMatchLevel.NO_MATCH,
        subject=subject,
        category=category,
        style=style,
        reason_code="no_matching_decorative_asset",
        display_text_ko="요청에 맞는 검증된 에셋 후보를 찾지 못했습니다. 추가 정보가 필요합니다.",
    )


def _ensure_unique_label_keys(
    rows: tuple[SubjectCategoryLabelRow, ...],
    label_type: str,
) -> None:
    seen: set[str] = set()
    for row in rows:
        if row.key in seen:
            raise ValueError(f"Duplicate {label_type} label key: {row.key}")
        seen.add(row.key)


def _label_for(
    rows: tuple[SubjectCategoryLabelRow, ...],
    key: str,
    *,
    label_type: str,
) -> str:
    for row in rows:
        if row.key == key:
            return row.label_ko
    raise ValueError(f"Missing {label_type} label for key: {key}")


def _with_josa_and(label: str) -> str:
    if not label:
        return label
    codepoint = ord(label[-1])
    if not (0xAC00 <= codepoint <= 0xD7A3):
        return f"{label}와"
    has_final_consonant = (codepoint - 0xAC00) % 28 != 0
    return f"{label}{'과' if has_final_consonant else '와'}"


__all__ = [
    "AssetCandidateExplanation",
    "AssetCandidateExplanationSource",
    "AssetCandidateMatchLevel",
    "DEFAULT_DECORATIVE_ASSET_CATALOG_PATH",
    "DEFAULT_SUBJECT_CATEGORY_LABELS_PATH",
    "DecorativeAssetCatalog",
    "DecorativeAssetEntry",
    "DecorativeAssetLegalReviewStatus",
    "DecorativeAssetPrintabilityStatus",
    "DecorativeAssetSelectionRequest",
    "DecorativeAssetSelectionResult",
    "DecorativeAssetSelectionStatus",
    "DecorativeAssetSelector",
    "DecorativeAssetSourceType",
    "SubjectCategoryLabelRow",
    "SubjectCategoryLabelTable",
    "decorative_asset_manifest_metadata",
    "load_decorative_asset_catalog",
    "load_subject_category_label_table",
]
