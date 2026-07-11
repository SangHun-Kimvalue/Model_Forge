"""Explicit runtime execution bridge for selected curated assets.

Phase 12W intentionally stopped at ``runtime_asset_selected`` so a user
choice could not silently start CAD/STL/Orca execution. Phase 12Y adds this
separate bridge: only after that boundary is recorded can a caller explicitly
execute the selected curated asset through the local manufacturing runtime.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from apps.orchestrator.dependencies import Dependencies
from apps.orchestrator.exceptions import SessionStateError
from apps.orchestrator.schemas import (
    IntakeNextStepStatus,
    RuntimeAssetExecutionRequest,
    RuntimeAssetExecutionResponse,
    SessionEventKind,
)
from apps.orchestrator.sessions import Session, make_event
from modules.artifacts import (
    ArtifactKind,
    ManifestStatus,
    SubtaskArtifactManifest,
    build_artifact_ref,
    get_artifact_root,
    write_failure_manifest,
    write_manifest,
)
from modules.cad_mechanical.schemas import CADDialect, GenerationRequest, SandboxLimits
from modules.newbie_request import (
    DecorativeAssetEntry,
    DecorativeAssetKeyringRenderer,
    DecorativeAssetSelectionRequest,
    DecorativeAssetSelectionResult,
    DecorativeAssetSelectionStatus,
    DecorativeAssetSelector,
    PrintabilityReport,
    RenderedAssembly,
    VisualQualityStatus,
    VisualReviewCriterion,
    VisualReviewRubricItem,
    apply_manual_visual_review,
    decorative_asset_manifest_metadata,
    evaluate_assembly_stl,
    evaluate_printability,
    is_visual_quality_product_ready,
    load_decorative_asset_catalog,
    render_stl_preview,
    require_printability_for_slicing,
)
from modules.newbie_request.schemas import L2GeometryReport
from modules.slicer.schemas import MaterialPreset, PrinterProfile, SliceRequest


async def execute_selected_runtime_asset(
    deps: Dependencies,
    session: Session,
    request: RuntimeAssetExecutionRequest,
) -> RuntimeAssetExecutionResponse:
    """Run a previously selected curated asset through CAD/STL/Orca.

    Preconditions:
        - ``/chat`` selected a natural-language curated asset route.
        - ``/intake-choice`` recorded ``use_existing_runtime_asset``.
        - ``/intake-next-step`` recorded ``runtime_asset_selected``.

    The response and manifest keep product/release approval explicitly false.
    """

    if request.session_id != session.session_id:
        raise SessionStateError("Runtime asset execution session id does not match.")

    route = session.latest_route_result
    next_step = session.latest_intake_next_step
    if route is None or next_step is None:
        raise SessionStateError(
            "Runtime asset execution requires a recorded intake next-step boundary."
        )
    if request.route_snapshot_id != route.route_snapshot_id:
        raise SessionStateError(
            "Runtime asset execution was requested against a stale route snapshot."
        )
    if next_step.route_snapshot_id != route.route_snapshot_id:
        raise SessionStateError(
            "Recorded intake next-step no longer matches the latest route snapshot."
        )
    if next_step.status is not IntakeNextStepStatus.RUNTIME_ASSET_SELECTED:
        raise SessionStateError(
            "Runtime asset execution is only allowed for runtime_asset_selected."
        )
    if next_step.runtime_execution_started:
        raise SessionStateError("Selected runtime asset has already been executed.")
    asset_id = next_step.runtime_asset_id or route.runtime_asset_id
    if not asset_id:
        raise SessionStateError("Runtime asset execution requires a runtime asset id.")

    catalog = load_decorative_asset_catalog()
    assets = catalog.by_id()
    if asset_id not in assets:
        raise SessionStateError(f"Runtime asset not found in catalog: {asset_id!r}.")
    asset = assets[asset_id]
    selection = DecorativeAssetSelector(catalog).select(
        DecorativeAssetSelectionRequest(
            request_id=asset.request_ids[0],
            subject=asset.subject,
            style=asset.style,
        )
    )
    if (
        selection.status is not DecorativeAssetSelectionStatus.SELECTED
        or selection.asset_id != asset_id
    ):
        raise SessionStateError(
            f"Runtime asset selector did not return selected asset {asset_id!r}."
        )

    artifact_root = get_artifact_root().resolve()
    execution_id = uuid4().hex[:12]
    route_hash = route.route_snapshot_id.split(":")[-1][:12]
    subtask_id = f"{asset_id}-{route_hash}-{execution_id}"
    output_dir = artifact_root / session.session_id / subtask_id
    output_dir.mkdir(parents=True, exist_ok=True)

    await deps.sessions.publish(
        session.session_id,
        make_event(
            session,
            SessionEventKind.SUBTASK_STARTED,
            payload={
                "subtask_id": subtask_id,
                "kind": "decorative_asset",
                "stage": "runtime_asset_execution",
                "runtime_asset_id": asset_id,
                "route_snapshot_id": route.route_snapshot_id,
            },
        ),
    )

    try:
        rendered = DecorativeAssetKeyringRenderer().render(asset)
        cad_result = await deps.cad.generate(
            GenerationRequest(
                prompt=str(route.requirement.get("user_prompt_ko") or asset.asset_id),
                code=rendered.source_code,
                dialect=CADDialect.OPENSCAD,
                session_id=session.session_id,
                output_dir=output_dir,
                format="stl",
                limits=SandboxLimits(timeout_s=60.0),
            )
        )
        l2_report = evaluate_assembly_stl(cad_result.path)
        if not l2_report.passed:
            raise SessionStateError("Runtime asset STL failed L2 geometry validation.")
        printability_report = evaluate_printability(l2_report)
        require_printability_for_slicing(printability_report)

        review_metadata = _runtime_visual_metadata(
            asset=asset,
            rendered=rendered,
            l2_report=l2_report,
            printability_report=printability_report,
            selection=selection,
            route_snapshot_id=route.route_snapshot_id,
        )

        slice_result = await deps.slicer.slice(
            SliceRequest(
                mesh_path=cad_result.path,
                mesh_format="stl",
                session_id=session.session_id,
                output_dir=output_dir,
                printer=_printer_from_deps(deps),
                material=_material_from_deps(deps),
                process_profile=deps.settings.default_process_profile,
            )
        )
        gcode_head = slice_result.gcode_path.read_text(
            encoding="utf-8", errors="replace"
        )[:4000]
        if "OrcaSlicer" not in gcode_head or "mock gcode" in gcode_head.lower():
            raise SessionStateError(
                "Runtime asset slicing did not produce real Orca G-code."
            )

        preview_path = output_dir / f"{asset_id}-preview.png"
        _write_stl_preview(cad_result.path, preview_path)

        artifact_metadata = {
            **review_metadata,
            "product_ready": is_visual_quality_product_ready(review_metadata),
            "release_allowed": review_metadata["release_allowed"],
            "runtime_execution_started": True,
            "runtime_catalog_registered": False,
            "phase": "12Y",
            "runtime_asset_execution": {
                "route_snapshot_id": route.route_snapshot_id,
                "execution_id": execution_id,
                "asset_id": asset_id,
                "slicer_adapter": slice_result.adapter_used,
                "slicer_version": slice_result.slicer_version,
                "gcode_size_bytes": slice_result.gcode_path.stat().st_size,
            },
        }
        artifact_refs = [
            build_artifact_ref(
                kind=ArtifactKind.MECHANICAL_MESH,
                path=cad_result.path,
                session_id=session.session_id,
                subtask_id=subtask_id,
                producer_adapter=cad_result.adapter_used,
                producer_version=_string_or_none(
                    cad_result.metadata.get("openscad_version")
                ),
                root=artifact_root,
                metadata=artifact_metadata,
            ),
            build_artifact_ref(
                kind=ArtifactKind.PREVIEW_IMAGE,
                path=preview_path,
                session_id=session.session_id,
                subtask_id=subtask_id,
                producer_adapter="phase-12y-lightweight-stl-preview",
                producer_version="0.1.0",
                root=artifact_root,
                metadata=artifact_metadata,
            ),
            build_artifact_ref(
                kind=ArtifactKind.GCODE,
                path=slice_result.gcode_path,
                session_id=session.session_id,
                subtask_id=subtask_id,
                producer_adapter=slice_result.adapter_used,
                producer_version=slice_result.slicer_version,
                root=artifact_root,
                metadata=artifact_metadata,
            )
        ]
        if slice_result.threemf_path is not None:
            artifact_refs.append(
                build_artifact_ref(
                    kind=ArtifactKind.THREEMF,
                    path=slice_result.threemf_path,
                    session_id=session.session_id,
                    subtask_id=subtask_id,
                    producer_adapter=slice_result.adapter_used,
                    producer_version=slice_result.slicer_version,
                    root=artifact_root,
                    metadata=artifact_metadata,
                )
            )
        manifest = SubtaskArtifactManifest(
            session_id=session.session_id,
            subtask_id=subtask_id,
            artifacts=tuple(artifact_refs),
            created_at=datetime.now(UTC),
            status=ManifestStatus.SUCCESS,
            trace_id=f"trace_12y_runtime_asset_{execution_id}",
        )
        manifest_path = write_manifest(manifest, root=artifact_root)
        manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        failure_path = write_failure_manifest(
            session_id=session.session_id,
            subtask_id=subtask_id,
            stage="runtime_asset_execution",
            error_type=type(exc).__name__,
            detail=str(exc) or type(exc).__name__,
            error_metadata={
                "runtime_asset_id": asset_id,
                "route_snapshot_id": route.route_snapshot_id,
                "execution_id": execution_id,
            },
            trace_id=session.trace_id,
            root=artifact_root,
        )
        await deps.sessions.publish(
            session.session_id,
            make_event(
                session,
                SessionEventKind.SUBTASK_FAILED,
                payload={
                    "subtask_id": subtask_id,
                    "kind": "decorative_asset",
                    "stage": "runtime_asset_execution",
                    "error_type": type(exc).__name__,
                    "detail": str(exc) or type(exc).__name__,
                    "runtime_asset_id": asset_id,
                    "route_snapshot_id": route.route_snapshot_id,
                    "manifest_path": str(failure_path),
                },
            ),
        )
        raise SessionStateError(
            "Runtime asset execution failed; diagnostic manifest was written."
        ) from exc

    updated_next_step = next_step.model_copy(
        update={
            "runtime_execution_started": True,
            "runtime_catalog_registered": False,
            "product_ready": False,
            "release_allowed": False,
            "message": (
                "선택된 검증 후보의 제조 runtime 검증을 완료했습니다. "
                "제품 품질/출시 검토는 아직 필요합니다."
            ),
        }
    )
    session.latest_intake_next_step = updated_next_step

    response = RuntimeAssetExecutionResponse(
        session_id=session.session_id,
        state=session.state,
        route_snapshot_id=route.route_snapshot_id,
        runtime_asset_id=asset_id,
        subtask_id=subtask_id,
        runtime_execution_started=True,
        runtime_catalog_registered=False,
        product_ready=False,
        release_allowed=False,
        next_step=updated_next_step,
        manifest=manifest_payload,
        message=updated_next_step.message,
    )

    await deps.sessions.publish(
        session.session_id,
        make_event(
            session,
            SessionEventKind.INTAKE_NEXT_STEP_RECORDED,
            payload=updated_next_step.model_dump(mode="json"),
        ),
    )
    await deps.sessions.publish(
        session.session_id,
        make_event(
            session,
            SessionEventKind.SUBTASK_COMPLETED,
            payload={
                "subtask_id": subtask_id,
                "kind": "decorative_asset",
                "runtime_asset_id": asset_id,
                "route_snapshot_id": route.route_snapshot_id,
                "manifest": manifest_payload,
            },
        ),
    )
    return response


def _runtime_visual_metadata(
    *,
    asset: DecorativeAssetEntry,
    rendered: RenderedAssembly,
    l2_report: L2GeometryReport,
    printability_report: PrintabilityReport,
    selection: DecorativeAssetSelectionResult,
    route_snapshot_id: str,
) -> dict[str, object]:
    metadata = decorative_asset_manifest_metadata(
        asset=asset,
        rendered=rendered,
        l2_report=l2_report,
        printability_report=printability_report,
        selection=selection,
        gcode_stage="generated",
        gcode_reason="phase_12y_explicit_runtime_asset_execution",
    )
    subject = getattr(asset, "subject", "")
    subject_score = 4 if subject == "bear" else 3
    return apply_manual_visual_review(
        metadata,
        status=VisualQualityStatus.MANUAL_PASS_CANDIDATE,
        reason="phase_12y_manufacturing_e2e_candidate",
        review_note=(
            "Selected curated asset can enter Phase 12Y manufacturing E2E, "
            "but this is not product visual PASS or release approval."
        ),
        rubric_items=(
            VisualReviewRubricItem(
                criterion=VisualReviewCriterion.SUBJECT_READABILITY,
                score=subject_score,
                passed=True,
                note=f"Subject is readable enough for route snapshot {route_snapshot_id}.",
            ),
            VisualReviewRubricItem(
                criterion=VisualReviewCriterion.PRESENTATION_READINESS,
                score=3,
                passed=False,
                note="Final product presentation review remains required.",
            ),
        ),
    )


def _printer_from_deps(deps: Dependencies) -> PrinterProfile:
    return PrinterProfile(
        name=deps.settings.default_printer_profile,
        nozzle_diameter_mm=deps.settings.default_nozzle_diameter_mm,
        bed_size_mm=deps.settings.default_bed_size_mm,
    )


def _material_from_deps(deps: Dependencies) -> MaterialPreset:
    return MaterialPreset(
        name=deps.settings.default_material_profile,
        nozzle_temp_c=deps.settings.default_nozzle_temp_c,
        bed_temp_c=deps.settings.default_bed_temp_c,
    )


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _write_stl_preview(stl_path: Path, preview_path: Path) -> None:
    render_stl_preview(stl_path, preview_path, size=(520, 320))
    if not preview_path.is_file() or preview_path.stat().st_size <= 0:
        raise SessionStateError("Runtime asset preview image was not created.")


__all__ = ["execute_selected_runtime_asset"]
