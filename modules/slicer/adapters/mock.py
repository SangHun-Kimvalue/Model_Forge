"""Deterministic mock slicer for unit tests and local scaffolding (DESIGN.md §4.5)."""

import hashlib
import math
import time

from modules.slicer.base import BaseSlicer
from modules.slicer.exceptions import SlicerConfigError, SlicerExecutionError
from modules.slicer.schemas import SliceRequest, SliceResult
from modules.slicer.threemf import (
    EXPECTED_THREEMF_SCHEMA_VERSION,
    read_threemf_manifest,
    write_minimal_threemf,
)


class MockSlicer(BaseSlicer):
    """Deterministic slicer adapter.

    Reads ``request.mesh_path`` as bytes, hashes the input to produce a
    stable artifact name, and writes:

    - a small G-code file with the pinned ``slicer_version``, layer
      markers, and end-of-print sentinel;
    - a minimal 3MF archive containing the source mesh as metadata and
      a schema-pinned ``3dmodel.model`` (round-trips through
      ``read_threemf_manifest``).

    Estimates are computed from a fake 20 mm-tall print so they are
    deterministic and unit-testable. They are not physically meaningful.
    """

    default_adapter_name = "mock-slicer-v1"
    default_slicer_version = "mock-1.0.0"

    _FAKE_HEIGHT_MM = 20.0
    _FAKE_SEC_PER_LAYER = 30.0

    def __init__(
        self,
        adapter_label: str | None = None,
        slicer_version: str | None = None,
    ) -> None:
        self._adapter_label = adapter_label or self.default_adapter_name
        self._slicer_version = slicer_version or self.default_slicer_version

    @property
    def adapter_name(self) -> str:
        return self._adapter_label

    @property
    def slicer_version(self) -> str:
        return self._slicer_version

    def health_check(self) -> bool:
        return True

    async def slice(self, request: SliceRequest) -> SliceResult:
        if not request.output_dir.exists() or not request.output_dir.is_dir():
            raise SlicerConfigError(
                f"output_dir '{request.output_dir}' is missing or not a directory."
            )
        if not request.mesh_path.exists() or not request.mesh_path.is_file():
            raise SlicerConfigError(
                f"mesh_path '{request.mesh_path}' is missing or not a file."
            )
        if request.mesh_format == "3mf":
            read_threemf_manifest(request.mesh_path)

        start = time.perf_counter()
        try:
            payload = request.mesh_path.read_bytes()
        except OSError as exc:
            raise SlicerExecutionError(
                f"failed to read mesh '{request.mesh_path}': {exc}"
            ) from exc

        digest = hashlib.sha256(payload).hexdigest()[:12]
        gcode_path = request.output_dir / f"mock-slice-{digest}.gcode"
        threemf_path = request.output_dir / f"mock-slice-{digest}.3mf"

        layer_count = max(1, math.ceil(self._FAKE_HEIGHT_MM / request.layer_height_mm))
        gcode_text = self._render_gcode(
            request=request, digest=digest, layer_count=layer_count
        )
        try:
            gcode_path.write_text(gcode_text, encoding="utf-8")
            write_minimal_threemf(
                threemf_path,
                mesh_payload=payload,
                mesh_filename=request.mesh_path.name,
                schema_version=EXPECTED_THREEMF_SCHEMA_VERSION,
            )
        except OSError as exc:
            raise SlicerExecutionError(
                f"failed to write slicer artifacts: {exc}"
            ) from exc

        estimated_filament_g = max(0.0, len(payload) / 1024.0) * 0.1

        return SliceResult(
            gcode_path=gcode_path,
            threemf_path=threemf_path,
            adapter_used=self._adapter_label,
            slicer_version=self._slicer_version,
            layer_count=layer_count,
            estimated_print_time_s=layer_count * self._FAKE_SEC_PER_LAYER,
            estimated_filament_g=estimated_filament_g,
            slicing_time_s=time.perf_counter() - start,
            metadata={
                "deterministic": True,
                "source_digest": digest,
                "printer": request.printer.name,
                "material": request.material.name,
                "layer_height_mm": request.layer_height_mm,
                "infill_percent": request.infill_percent,
                "schema_version": EXPECTED_THREEMF_SCHEMA_VERSION,
            },
        )

    def _render_gcode(
        self, request: SliceRequest, digest: str, layer_count: int
    ) -> str:
        header = [
            f"; Model Forge mock slicer {self._slicer_version}",
            f"; session_id={request.session_id}",
            f"; printer={request.printer.name}",
            f"; material={request.material.name}",
            f"; layer_height_mm={request.layer_height_mm}",
            f"; infill_percent={request.infill_percent}",
            f"; source_digest={digest}",
            f"M104 S{request.material.nozzle_temp_c}",
            f"M140 S{request.material.bed_temp_c}",
        ]
        body = [f"; LAYER:{i}" for i in range(layer_count)]
        footer = ["M104 S0", "M140 S0", "; END_OF_PRINT"]
        return "\n".join([*header, *body, *footer]) + "\n"


__all__ = ["MockSlicer"]
