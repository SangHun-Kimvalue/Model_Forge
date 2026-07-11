from __future__ import annotations

import hashlib

from modules.newbie_request.asset_catalog import (
    DecorativeAssetSelector,
    DecorativeAssetSourceType,
)
from modules.newbie_request.asset_draft_schemas import (
    DraftAssetAuthoringRequest,
    DraftAssetAuthoringResult,
    DraftAssetAuthoringStatus,
    DraftAssetEntry,
)
from modules.newbie_request.asset_intake import (
    AssetIntakeRequest,
    AssetIntakeResolver,
    AssetIntakeResult,
    AssetIntakeStatus,
)
from modules.newbie_request.printability import (
    PrintabilityReport,
    printability_gcode_metadata,
    printability_manifest_metadata,
)
from modules.newbie_request.schemas import NewbieRoute, RenderedAssembly, SizeMM


class AssetAuthoringError(ValueError):
    """Raised when a draft asset cannot be generated deterministically."""


class DeterministicCatKeyringDraftGenerator:
    generator_name = "deterministic_cat_keyring_draft_generator"
    renderer_version = "0.1.0"

    def create_draft(self, request: DraftAssetAuthoringRequest) -> DraftAssetEntry:
        subject = request.subject or _infer_subject(request.user_prompt_ko)
        category = request.category or _infer_category(request.user_prompt_ko)
        if subject != "cat" or category != "keyring":
            raise AssetAuthoringError("Only cat keyring draft authoring is supported")
        style = request.style or "round_face"
        digest = hashlib.sha256(
            f"{request.user_prompt_ko}|{subject}|{category}|{style}".encode()
        ).hexdigest()[:12]
        return DraftAssetEntry(
            draft_asset_id=f"draft_cat_keyring_{style}_{digest}",
            subject=subject,
            category=category,
            style=style,
            source_type=DecorativeAssetSourceType.SELF_AUTHORED,
            license="Model Forge-internal-self-authored-draft",
            source_url_or_owner="External Runtime/Model Forge",
            provenance_note=(
                "Deterministic local draft generated from Model Forge-authored "
                "OpenSCAD primitives; no external SVG/STL/commercial artwork used."
            ),
            originating_prompt=request.user_prompt_ko,
            draft_generator=self.generator_name,
            draft_renderer_version=self.renderer_version,
            review_queue_reason=request.review_queue_reason,
            default_size_mm=SizeMM(width=56.0, depth=54.0, height=4.2),
            min_size_mm=SizeMM(width=45.0, depth=42.0, height=3.0),
            recommended_thickness_mm=4.2,
            required_features=(
                "cat_head",
                "ears",
                "eyes",
                "nose_or_muzzle",
                "whiskers",
                "keyring_hole",
            ),
        )


class DraftAssetKeyringRenderer:
    renderer_name = "draft_asset_keyring_authoring_mvp"
    renderer_version = "0.1.0"

    def render(self, draft: DraftAssetEntry) -> RenderedAssembly:
        if draft.subject != "cat" or draft.category != "keyring":
            raise AssetAuthoringError("DraftAssetKeyringRenderer only supports cat keyrings")
        source = _cat_keyring_source(draft)
        return RenderedAssembly(
            source_catalog_id=draft.draft_asset_id,
            object_type=f"{draft.subject}_keyring",
            category=draft.category,
            source_code=source,
            renderer_name=self.renderer_name,
            renderer_version=self.renderer_version,
            metadata={
                "route": "draft_asset",
                "draft_asset_id": draft.draft_asset_id,
                "asset_lifecycle_status": draft.asset_lifecycle_status.value,
                "subject": draft.subject,
                "style": draft.style,
                "source_type": draft.source_type.value,
                "license": draft.license,
                "source_url_or_owner": draft.source_url_or_owner,
                "provenance_note": draft.provenance_note,
                "draft_generator": draft.draft_generator,
                "draft_renderer_version": draft.draft_renderer_version,
                "review_queue_reason": draft.review_queue_reason,
                "external_asset": False,
                "runtime_catalog_registered": draft.runtime_catalog_registered,
                "visual_quality_status": draft.visual_quality_status.value,
                "legal_review_status": draft.legal_review_status.value,
                "printability_status": draft.printability_status.value,
                "release_allowed": draft.release_allowed,
            },
        )


class AssetAuthoringPipeline:
    """Separates unsupported decorative requests from product runtime fallback."""

    def __init__(
        self,
        runtime_selector: DecorativeAssetSelector,
        *,
        draft_generator: DeterministicCatKeyringDraftGenerator | None = None,
        intake_resolver: AssetIntakeResolver | None = None,
    ) -> None:
        self._draft_generator = draft_generator or DeterministicCatKeyringDraftGenerator()
        self._intake_resolver = intake_resolver or AssetIntakeResolver(
            runtime_selector
        )

    def handle(
        self,
        request: DraftAssetAuthoringRequest,
    ) -> DraftAssetAuthoringResult:
        subject = request.subject or _infer_subject(request.user_prompt_ko)
        category = request.category or _infer_category(request.user_prompt_ko)
        style = request.style or _default_style(subject=subject, category=category)
        intake = self._intake_resolver.resolve(
            AssetIntakeRequest(
                user_prompt_ko=request.user_prompt_ko,
                request_id=None,
                subject=subject,
                category=category,
                style=style,
            )
        )
        if intake.status is AssetIntakeStatus.RUNTIME_CATALOG_MATCH:
            return DraftAssetAuthoringResult(
                status=DraftAssetAuthoringStatus.RUNTIME_CATALOG_AVAILABLE,
                reason="verified_runtime_catalog_asset_available",
                runtime_asset_id=intake.runtime_asset_id,
                intake_status=intake.status.value,
                intake_reason=intake.reason.value,
                intake_new_draft_allowed=intake.new_draft_allowed,
                intake_metadata=_intake_metadata(intake),
                candidate_asset_ids=intake.candidate_asset_ids,
                selected_route=NewbieRoute.CURATED_ASSET,
            )
        if intake.status is AssetIntakeStatus.DRAFT_QUEUE_MATCH:
            return DraftAssetAuthoringResult(
                status=DraftAssetAuthoringStatus.DRAFT_REUSE_AVAILABLE,
                reason="existing_draft_asset_available_before_authoring",
                draft_asset=intake.draft_asset,
                intake_status=intake.status.value,
                intake_reason=intake.reason.value,
                intake_new_draft_allowed=intake.new_draft_allowed,
                intake_metadata=_intake_metadata(intake),
                selected_route=NewbieRoute.DRAFT_ASSET,
            )
        if intake.status is AssetIntakeStatus.DUPLICATE_OR_SIMILAR_CANDIDATE:
            return DraftAssetAuthoringResult(
                status=DraftAssetAuthoringStatus.SIMILAR_REFERENCE_AVAILABLE,
                reason="similar_reference_candidate_requires_review_before_new_draft",
                reference_candidate_ids=intake.reference_candidate_ids,
                intake_status=intake.status.value,
                intake_reason=intake.reason.value,
                intake_new_draft_allowed=intake.new_draft_allowed,
                intake_metadata=_intake_metadata(intake),
                selected_route=NewbieRoute.ASK_USER,
            )

        if subject == "cat" and category == "keyring":
            draft = self._draft_generator.create_draft(
                request.model_copy(
                    update={
                        "subject": subject,
                        "category": category,
                        "style": style,
                    }
                )
            )
            return DraftAssetAuthoringResult(
                status=DraftAssetAuthoringStatus.DRAFT_CREATED,
                reason="unsupported_request_routed_to_draft_authoring",
                draft_asset=draft,
                intake_status=intake.status.value,
                intake_reason=intake.reason.value,
                intake_new_draft_allowed=intake.new_draft_allowed,
                intake_metadata=_intake_metadata(intake),
                selected_route=NewbieRoute.DRAFT_ASSET,
            )

        return DraftAssetAuthoringResult(
            status=DraftAssetAuthoringStatus.ASK_USER,
            reason="unsupported_request_requires_clarification_or_manual_review",
            intake_status=intake.status.value,
            intake_reason=intake.reason.value,
            intake_new_draft_allowed=intake.new_draft_allowed,
            intake_metadata=_intake_metadata(intake),
            selected_route=NewbieRoute.ASK_USER,
        )


def draft_asset_manifest_metadata(
    *,
    draft: DraftAssetEntry,
    rendered: RenderedAssembly,
    l2_report: object | None = None,
    printability_report: PrintabilityReport | None = None,
    gcode_stage: str = "not_run",
    gcode_reason: str = "draft_asset_review_queue_not_runtime_catalog",
) -> dict[str, object]:
    return {
        "route": "draft_asset",
        "draft_asset": draft.model_dump(mode="json"),
        "source_catalog_id": None,
        "draft_asset_id": draft.draft_asset_id,
        "asset_lifecycle_status": draft.asset_lifecycle_status.value,
        "originating_prompt": draft.originating_prompt,
        "draft_generator": draft.draft_generator,
        "draft_renderer_version": draft.draft_renderer_version,
        "review_queue_reason": draft.review_queue_reason,
        "renderer": rendered.renderer_name,
        "renderer_version": rendered.renderer_version,
        "renderer_metadata": dict(rendered.metadata),
        "visual_quality_required": True,
        "visual_quality_status": draft.visual_quality_status.value,
        "legal_review_status": draft.legal_review_status.value,
        "printability_status": draft.printability_status.value,
        "release_allowed": draft.release_allowed,
        "runtime_catalog_registered": draft.runtime_catalog_registered,
        "product_ready": False,
        "l2_geometry": l2_report.model_dump(mode="json")
        if hasattr(l2_report, "model_dump")
        else l2_report,
        "printability": printability_manifest_metadata(printability_report),
        "gcode": printability_gcode_metadata(
            printability_report,
            requested_stage=gcode_stage,
            requested_reason=gcode_reason,
        ),
    }


def _intake_metadata(intake: AssetIntakeResult) -> dict[str, object]:
    metadata = dict(intake.metadata)
    if intake.candidate_explanations:
        metadata["candidate_explanations"] = [
            explanation.model_dump(mode="json")
            for explanation in intake.candidate_explanations
        ]
    return metadata


def _infer_subject(prompt: str) -> str | None:
    normalized = prompt.casefold()
    if "고양이" in normalized or "cat" in normalized:
        return "cat"
    return None


def _infer_category(prompt: str) -> str | None:
    normalized = prompt.casefold()
    if "키링" in normalized or "keyring" in normalized:
        return "keyring"
    return None


def _default_style(*, subject: str | None, category: str | None) -> str | None:
    if subject == "cat" and category == "keyring":
        return "round_face"
    return None


def _cat_keyring_source(draft: DraftAssetEntry) -> str:
    base_h = draft.recommended_thickness_mm
    relief_h = 1.25
    return "\n".join(
        [
            "$fn = 96;",
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
            "module raised_disc(x, y, r, z, h) {",
            "  translate([x, y, z]) cylinder(h=h, r=r);",
            "}",
            "",
            "module raised_capsule(x1, y1, x2, y2, r, z, h) {",
            "  translate([0, 0, z]) linear_extrude(height=h)",
            "    capsule_2d(x1, y1, x2, y2, r);",
            "}",
            "",
            "module body_2d() {",
            "  union() {",
            "    ellipse_2d(28.000, 25.000, 21.500, 19.800);",
            "    polygon(points=[[12.0,39.0],[19.2,55.0],[24.5,39.6]]);",
            "    polygon(points=[[31.5,39.6],[36.8,55.0],[44.0,39.0]]);",
            "    capsule_2d(28.000, 44.000, 28.000, 51.500, 3.700);",
            "  }",
            "}",
            "",
            "module face_relief() {",
            f"  raised_disc(22.500, 27.600, 1.400, {base_h - 0.04:.3f}, {relief_h:.3f});",
            f"  raised_disc(33.500, 27.600, 1.400, {base_h - 0.04:.3f}, {relief_h:.3f});",
            f"  raised_disc(28.000, 22.700, 1.250, {base_h + 0.85:.3f}, 0.760);",
            f"  raised_capsule(16.300, 23.300, 23.600, 24.600, 0.330, {base_h + 0.78:.3f}, 0.560);",
            f"  raised_capsule(32.400, 24.600, 39.700, 23.300, 0.330, {base_h + 0.78:.3f}, 0.560);",
            f"  raised_capsule(17.100, 20.400, 23.600, 21.700, 0.300, {base_h + 0.78:.3f}, 0.520);",
            f"  raised_capsule(32.400, 21.700, 38.900, 20.400, 0.300, {base_h + 0.78:.3f}, 0.520);",
            "}",
            "",
            "module main() {",
            "  difference() {",
            "    union() {",
            f"      linear_extrude(height={base_h:.3f}) body_2d();",
            "      face_relief();",
            "    }",
            f"    translate([28.000, 51.800, -0.500]) "
            f"cylinder(h={base_h + relief_h + 1.0:.3f}, r=1.700);",
            "  }",
            "}",
            "",
            "main();",
        ]
    )


__all__ = [
    "AssetAuthoringError",
    "AssetAuthoringPipeline",
    "DeterministicCatKeyringDraftGenerator",
    "DraftAssetKeyringRenderer",
    "draft_asset_manifest_metadata",
]
