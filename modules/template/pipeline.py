"""RecipePipeline: template-driven mechanical CAD -> validate -> slice pipeline.

Compared to :class:`~apps.orchestrator.pipeline.MechanicalPipeline` this
variant skips the LLM coder step — the rendered code is deterministic and
comes from :class:`~modules.template.renderer.RecipeRenderer`.

ADR-0008 mandates that every printable artifact produced by this pipeline carries
``recipe_id``, ``recipe_version``, ``param_hash``, and ``code_hash`` in
the artifact metadata field so the manifest is the
single source of truth for which recipe and parameter values produced the
artifact (ADR-0005 + ADR-0008 invariants).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from modules.artifacts import (
    ArtifactKind,
    SubtaskArtifactManifest,
    build_artifact_ref,
    write_manifest,
)
from modules.cad_mechanical.base import BaseMechanicalCADGenerator
from modules.cad_mechanical.schemas import CADDialect, GenerationRequest
from modules.slicer.base import BaseSlicer
from modules.slicer.schemas import MaterialPreset, PrinterProfile, SliceRequest
from modules.template.loader import RecipeLoader
from modules.template.renderer import RecipeRenderer
from modules.validator.base import BaseMeshValidator
from modules.validator.schemas import ValidationCheck, ValidationRequest


@dataclass(frozen=True)
class RecipePipelineArtifacts:
    """Result bundle for a recipe execution.

    ``manifest`` is the ADR-0005 manifest; consumers must read artifact
    paths from ``manifest.artifacts[].relative_uri`` only (ADR-0005 §5).
    """

    recipe_id: str
    recipe_version: str
    adapter_cad: str
    adapter_slicer: str
    manifest: SubtaskArtifactManifest

    def to_event_payload(self) -> dict[str, object]:
        return {
            "recipe_id": self.recipe_id,
            "recipe_version": self.recipe_version,
            "adapter_cad": self.adapter_cad,
            "adapter_slicer": self.adapter_slicer,
            "manifest": self.manifest.model_dump(mode="json"),
        }


@dataclass(frozen=True)
class RecipePipeline:
    """Template-driven pipeline: render -> CAD -> validate -> slice.

    No LLM coder is involved; the source code is deterministically
    rendered from the recipe template by :class:`RecipeRenderer`.

    Adapters are the same as :class:`~apps.orchestrator.pipeline.MechanicalPipeline`
    so the two pipelines can share the same dependency-injection setup.
    """

    loader: RecipeLoader
    renderer: RecipeRenderer
    cad: BaseMechanicalCADGenerator
    validator: BaseMeshValidator
    slicer: BaseSlicer
    logger: logging.Logger
    printer: PrinterProfile
    material: MaterialPreset

    async def execute(
        self,
        *,
        recipe_id: str,
        parameters: dict[str, Any] | None = None,
        session_id: str,
        subtask_id: str,
        output_dir: Path,
        artifact_root: Path,
        trace_id: str | None = None,
        approval_gate_id: str | None = None,
        plan_snapshot_id: str | None = None,
    ) -> RecipePipelineArtifacts:
        """Execute the recipe pipeline end-to-end.

        Parameters
        ----------
        recipe_id:
            The recipe to execute (must exist in the loader's catalog).
        parameters:
            User-supplied parameter overrides.  Falls back to defaults.
        session_id, subtask_id:
            Identifiers for artifact path construction and logging.
        output_dir:
            Must exist and be writable before this method is called.
        artifact_root:
            Root passed to :func:`~modules.artifacts.build_artifact_ref`.
        trace_id, approval_gate_id, plan_snapshot_id:
            Optional provenance fields recorded in the manifest.

        Raises
        ------
        RecipeNotFoundError / RecipeLoadError
            If the recipe cannot be loaded.
        RecipeRenderError
            If parameters fail validation.
        RuntimeError
            If mesh validation fails.
        """
        recipe = self.loader.load(recipe_id)
        rendered = self.renderer.render(recipe, parameters)

        self.logger.info(
            "recipe_rendered",
            extra={
                "session_id": session_id,
                "subtask_id": subtask_id,
                "recipe_id": rendered.recipe_id,
                "recipe_version": rendered.recipe_version,
                "param_hash": rendered.param_hash,
                "code_hash": rendered.code_hash,
            },
        )

        cad_result = await self.cad.generate(
            GenerationRequest(
                prompt=f"recipe:{recipe_id}",
                code=rendered.code,
                dialect=CADDialect(recipe.manifest.dialect),
                session_id=session_id,
                output_dir=output_dir,
                format="stl",
            )
        )
        self.logger.info(
            "recipe_cad_completed",
            extra={
                "session_id": session_id,
                "subtask_id": subtask_id,
                "stl_path": str(cad_result.path),
                "adapter": cad_result.adapter_used,
            },
        )

        report = await self.validator.validate(
            ValidationRequest(
                mesh_path=cad_result.path,
                mesh_format="stl",
                session_id=session_id,
                checks=frozenset(
                    {
                        ValidationCheck.MANIFOLD,
                        ValidationCheck.WALL_THICKNESS,
                        ValidationCheck.NORMAL_CONSISTENCY,
                    }
                ),
            )
        )
        if not report.passed:
            error_issues = [i for i in report.issues if i.severity.value == "error"]
            raise RuntimeError(
                f"Mesh validation failed for recipe '{recipe_id}': "
                + "; ".join(i.message for i in error_issues)
            )
        self.logger.info(
            "recipe_validation_passed",
            extra={
                "session_id": session_id,
                "subtask_id": subtask_id,
                "triangle_count": report.triangle_count,
            },
        )

        slicer_hints = recipe.manifest.slicer_hints
        layer_height_mm = (
            slicer_hints.layer_height_mm
            if slicer_hints.layer_height_mm is not None
            else 0.2
        )
        infill_percent = (
            slicer_hints.infill_percent
            if slicer_hints.infill_percent is not None
            else 20
        )

        slice_result = await self.slicer.slice(
            SliceRequest(
                mesh_path=cad_result.path,
                mesh_format="stl",
                session_id=session_id,
                output_dir=output_dir,
                printer=self.printer,
                material=self.material,
                layer_height_mm=layer_height_mm,
                infill_percent=infill_percent,
            )
        )
        self.logger.info(
            "recipe_slice_completed",
            extra={
                "session_id": session_id,
                "subtask_id": subtask_id,
                "gcode_path": str(slice_result.gcode_path),
                "adapter": slice_result.adapter_used,
            },
        )

        manifest = self._build_manifest(
            session_id=session_id,
            subtask_id=subtask_id,
            artifact_root=artifact_root,
            cad_path=cad_result.path,
            cad_adapter=cad_result.adapter_used,
            gcode_path=slice_result.gcode_path,
            threemf_path=slice_result.threemf_path,
            slicer_adapter=slice_result.adapter_used,
            slicer_version=slice_result.slicer_version,
            recipe_id=rendered.recipe_id,
            recipe_version=rendered.recipe_version,
            param_hash=rendered.param_hash,
            code_hash=rendered.code_hash,
            trace_id=trace_id,
            approval_gate_id=approval_gate_id,
            plan_snapshot_id=plan_snapshot_id,
        )
        write_manifest(manifest, root=artifact_root)
        self.logger.info(
            "recipe_manifest_written",
            extra={
                "session_id": session_id,
                "subtask_id": subtask_id,
                "artifact_count": len(manifest.artifacts),
            },
        )

        return RecipePipelineArtifacts(
            recipe_id=rendered.recipe_id,
            recipe_version=rendered.recipe_version,
            adapter_cad=cad_result.adapter_used,
            adapter_slicer=slice_result.adapter_used,
            manifest=manifest,
        )

    def _build_manifest(
        self,
        *,
        session_id: str,
        subtask_id: str,
        artifact_root: Path,
        cad_path: Path,
        cad_adapter: str,
        gcode_path: Path,
        threemf_path: Path | None,
        slicer_adapter: str,
        slicer_version: str,
        recipe_id: str,
        recipe_version: str,
        param_hash: str,
        code_hash: str,
        trace_id: str | None,
        approval_gate_id: str | None,
        plan_snapshot_id: str | None,
    ) -> SubtaskArtifactManifest:
        recipe_meta: dict[str, str] = {
            "recipe_id": recipe_id,
            "recipe_version": recipe_version,
            "param_hash": param_hash,
            "rendered_code_hash": code_hash,
        }
        refs = [
            build_artifact_ref(
                kind=ArtifactKind.MECHANICAL_MESH,
                path=cad_path,
                session_id=session_id,
                subtask_id=subtask_id,
                producer_adapter=cad_adapter,
                root=artifact_root,
                metadata=recipe_meta,
            ),
            build_artifact_ref(
                kind=ArtifactKind.GCODE,
                path=gcode_path,
                session_id=session_id,
                subtask_id=subtask_id,
                producer_adapter=slicer_adapter,
                producer_version=slicer_version,
                root=artifact_root,
                metadata=recipe_meta,
            ),
        ]
        if threemf_path is not None:
            refs.append(
                build_artifact_ref(
                    kind=ArtifactKind.THREEMF,
                    path=threemf_path,
                    session_id=session_id,
                    subtask_id=subtask_id,
                    producer_adapter=slicer_adapter,
                    producer_version=slicer_version,
                    root=artifact_root,
                    metadata=recipe_meta,
                )
            )
        return SubtaskArtifactManifest(
            session_id=session_id,
            subtask_id=subtask_id,
            artifacts=tuple(refs),
            created_at=datetime.now(UTC),
            trace_id=trace_id,
            approval_gate_id=approval_gate_id,
            plan_snapshot_id=plan_snapshot_id,
        )


__all__ = [
    "RecipePipeline",
    "RecipePipelineArtifacts",
]
