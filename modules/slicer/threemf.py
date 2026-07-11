"""Structured 3MF reader/writer used by mock and real slicer adapters.

3MF is a ZIP archive containing XML documents (DESIGN.md §4.5). The full
spec is intentionally large; this module implements only the slices we
need at the harness level — manifest parsing for schema-version pinning
and a minimal-writer for the mock adapter. Real adapters (e.g. OrcaSlicer
output) will validate richer parts on top of this contract.

The pin (``EXPECTED_THREEMF_SCHEMA_VERSION``) reflects R8: slicer/3MF
schema drift between versions is treated as a config error, not a silent
upgrade. Override via env ``SLICER_3MF_SCHEMA_VERSION`` when the build
moves to a new pinned version.
"""

import os
import xml.etree.ElementTree as ET  # noqa: S405 — parsing our own zip-confined XML
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from modules.slicer.exceptions import (
    ThreeMFSchemaError,
    UnsupportedSchemaVersionError,
)

THREEMF_CORE_NAMESPACE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
THREEMF_MODEL_PATH = "3D/3dmodel.model"
EXPECTED_THREEMF_SCHEMA_VERSION = os.environ.get(
    "SLICER_3MF_SCHEMA_VERSION", "1.0"
)
_DETERMINISTIC_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_DETERMINISTIC_FILE_MODE = 0o644 << 16


@dataclass(frozen=True)
class ThreeMFManifest:
    """Parsed 3MF manifest (subset used for schema-version pinning)."""

    schema_version: str
    model_unit: str
    object_count: int
    namespaces: tuple[str, ...] = field(default_factory=tuple)


def _write_deterministic_part(
    archive: zipfile.ZipFile, filename: str, payload: str | bytes
) -> None:
    """Write a zip part without host clock or platform metadata drift."""
    info = zipfile.ZipInfo(filename, date_time=_DETERMINISTIC_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = _DETERMINISTIC_FILE_MODE
    archive.writestr(info, payload)


def read_threemf_manifest(path: Path) -> ThreeMFManifest:
    """Open ``path`` as a 3MF archive and return its parsed manifest.

    Raises:
        ThreeMFSchemaError: archive is not a valid 3MF (missing parts,
            malformed XML).
        UnsupportedSchemaVersionError: the embedded schema version does
            not match ``EXPECTED_THREEMF_SCHEMA_VERSION``.
    """

    if not path.exists() or not path.is_file():
        raise ThreeMFSchemaError(f"3MF path '{path}' is missing or not a file")
    if not zipfile.is_zipfile(path):
        raise ThreeMFSchemaError(f"'{path}' is not a ZIP archive")

    with zipfile.ZipFile(path, "r") as archive:
        try:
            with archive.open(THREEMF_MODEL_PATH) as fh:
                payload = fh.read()
        except KeyError as exc:
            raise ThreeMFSchemaError(
                f"3MF archive missing '{THREEMF_MODEL_PATH}' part."
            ) from exc

    try:
        root = ET.fromstring(payload)  # noqa: S314 — confined to zip we just opened
    except ET.ParseError as exc:
        raise ThreeMFSchemaError(f"3MF model XML is malformed: {exc}") from exc

    schema_version = root.attrib.get("schemaversion") or root.attrib.get(
        f"{{{THREEMF_CORE_NAMESPACE}}}schemaversion"
    )
    if schema_version is None:
        raise ThreeMFSchemaError(
            "3MF root <model> element is missing the 'schemaversion' attribute."
        )
    if schema_version != EXPECTED_THREEMF_SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            f"3MF schema version '{schema_version}' does not match the pinned "
            f"version '{EXPECTED_THREEMF_SCHEMA_VERSION}'. Update the pin "
            "(SLICER_3MF_SCHEMA_VERSION) and the adapter contract before slicing."
        )

    model_unit = root.attrib.get("unit", "millimeter")
    resources = root.find(f"{{{THREEMF_CORE_NAMESPACE}}}resources")
    object_count = (
        len(resources.findall(f"{{{THREEMF_CORE_NAMESPACE}}}object"))
        if resources is not None
        else 0
    )

    return ThreeMFManifest(
        schema_version=schema_version,
        model_unit=model_unit,
        object_count=object_count,
        namespaces=(THREEMF_CORE_NAMESPACE,),
    )


def write_minimal_threemf(
    path: Path,
    *,
    mesh_payload: bytes,
    mesh_filename: str,
    schema_version: str = EXPECTED_THREEMF_SCHEMA_VERSION,
    model_unit: str = "millimeter",
) -> None:
    """Write a minimal valid 3MF that the mock slicer can reference.

    The archive contains a single ``<object>`` whose embedded mesh is
    stored as a separate part for traceability. Real adapters write
    much richer models — this is just enough to round-trip through
    ``read_threemf_manifest``.
    """

    model_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<model unit="{model_unit}" xml:lang="en-US" '
        f'xmlns="{THREEMF_CORE_NAMESPACE}" schemaversion="{schema_version}">\n'
        "  <resources>\n"
        '    <object id="1" type="model"/>\n'
        "  </resources>\n"
        "  <build>\n"
        '    <item objectid="1"/>\n'
        "  </build>\n"
        "</model>\n"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        '  <Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        '  <Default Extension="model" '
        'ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
        "</Types>\n"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        '  <Relationship Target="/3D/3dmodel.model" Id="rel0" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
        "</Relationships>\n"
    )

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _write_deterministic_part(archive, "[Content_Types].xml", content_types)
        _write_deterministic_part(archive, "_rels/.rels", rels)
        _write_deterministic_part(archive, THREEMF_MODEL_PATH, model_xml)
        _write_deterministic_part(
            archive, f"Metadata/source/{mesh_filename}", mesh_payload
        )


__all__ = [
    "EXPECTED_THREEMF_SCHEMA_VERSION",
    "THREEMF_CORE_NAMESPACE",
    "THREEMF_MODEL_PATH",
    "ThreeMFManifest",
    "read_threemf_manifest",
    "write_minimal_threemf",
]
