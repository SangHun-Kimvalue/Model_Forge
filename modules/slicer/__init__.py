"""Module D — Slicer (DESIGN.md §4.5).

Mesh → G-code via a headless slicer CLI. Phase 5 ships the contract,
mock adapter, and a structured 3MF helper. The Orca adapter scaffold is
present but refuses execution until the slicer image and version are
pinned (R8).
"""

from modules.slicer.adapters.mock import MockSlicer
from modules.slicer.adapters.orca_supervised import OrcaSupervisedSlicer
from modules.slicer.base import BaseSlicer
from modules.slicer.exceptions import (
    SlicerConfigError,
    SlicerError,
    SlicerExecutionError,
    SlicerVersionError,
    ThreeMFSchemaError,
    UnsupportedSchemaVersionError,
)
from modules.slicer.factory import SlicerSettings, create_slicer
from modules.slicer.orca_datadir import (
    DEFAULT_GENERIC_PRINTER_MATERIAL_PROFILES,
    DEFAULT_GENERIC_PRINTER_PRINTER_PROFILES,
    DEFAULT_GENERIC_PRINTER_PROCESS_PROFILES,
    OrcaDatadirTemplateIdentity,
    validate_orca_datadir_template,
)
from modules.slicer.schemas import (
    MaterialPreset,
    MeshInputFormat,
    PrinterProfile,
    SliceRequest,
    SliceResult,
)
from modules.slicer.threemf import (
    EXPECTED_THREEMF_SCHEMA_VERSION,
    ThreeMFManifest,
    read_threemf_manifest,
    write_minimal_threemf,
)

__all__ = [
    "EXPECTED_THREEMF_SCHEMA_VERSION",
    "DEFAULT_GENERIC_PRINTER_MATERIAL_PROFILES",
    "DEFAULT_GENERIC_PRINTER_PRINTER_PROFILES",
    "DEFAULT_GENERIC_PRINTER_PROCESS_PROFILES",
    "BaseSlicer",
    "MaterialPreset",
    "MeshInputFormat",
    "MockSlicer",
    "OrcaSupervisedSlicer",
    "OrcaDatadirTemplateIdentity",
    "PrinterProfile",
    "SliceRequest",
    "SliceResult",
    "SlicerConfigError",
    "SlicerError",
    "SlicerExecutionError",
    "SlicerSettings",
    "SlicerVersionError",
    "ThreeMFManifest",
    "ThreeMFSchemaError",
    "UnsupportedSchemaVersionError",
    "create_slicer",
    "read_threemf_manifest",
    "validate_orca_datadir_template",
    "write_minimal_threemf",
]
