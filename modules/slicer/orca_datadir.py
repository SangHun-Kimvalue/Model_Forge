"""Validation helpers for supervised Orca datadir templates.

ADR-0009 treats Orca as an external GUI/MCP runtime. The supervised adapter
must start each job from a clean, reproducible datadir template instead of a
developer's mutable user profile. This module pins the minimum structure and
identity checks before the process supervisor exists.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from modules.slicer.exceptions import SlicerConfigError

DEFAULT_GENERIC_PRINTER_PRINTER_PROFILES: tuple[str, ...] = (
    "Generic Printer standard-I 0.4 nozzle",
    "Generic Printer standard-Mini 0.4 nozzle",
    "Generic Printer standard-Plus 0.4 nozzle",
)
DEFAULT_GENERIC_PRINTER_MATERIAL_PROFILES: tuple[str, ...] = (
    "Generic Printer PLA @Generic Printer standard-Mini 0.4 nozzle",
)
DEFAULT_GENERIC_PRINTER_PROCESS_PROFILES: tuple[str, ...] = (
    "generic_printer default @Generic Printer standard-Mini 0.4 nozzle",
)

_CONF_FILENAME = "OrcaSlicer.conf"
_CHECKSUM_MARKER_RE = re.compile(r"\n# MD5 checksum [0-9A-Fa-f]+\s*$")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"[A-Za-z]:\\")
_VOLATILE_APP_KEYS = frozenset(
    {
        "download_path",
        "last_backup_path",
        "slicer_uuid",
        "window_layout",
    }
)


@dataclass(frozen=True)
class OrcaDatadirTemplateIdentity:
    """Stable identity for a clean Orca datadir template."""

    root: Path
    sha256: str
    file_count: int
    byte_count: int
    header: str
    printer_profiles: tuple[str, ...]
    material_profiles: tuple[str, ...]
    process_profiles: tuple[str, ...]


def _conf_payload(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SlicerConfigError(f"Unable to read Orca datadir config: {path}") from exc

    body = _CHECKSUM_MARKER_RE.sub("", raw)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SlicerConfigError(f"OrcaSlicer.conf is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SlicerConfigError("OrcaSlicer.conf root must be a JSON object.")
    return payload


def _iter_files(root: Path) -> tuple[Path, ...]:
    try:
        return tuple(sorted(path for path in root.rglob("*") if path.is_file()))
    except OSError as exc:
        raise SlicerConfigError(f"Unable to enumerate Orca datadir template: {root}") from exc


def _hash_datadir(root: Path, files: tuple[Path, ...]) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    for path in files:
        rel = path.relative_to(root).as_posix()
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise SlicerConfigError(f"Unable to read Orca datadir file: {path}") from exc
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
        byte_count += len(data)
    return digest.hexdigest(), byte_count


def _printer_profile_name(model: dict[str, Any]) -> str | None:
    vendor = str(model.get("vendor", "")).strip()
    name = str(model.get("model", "")).strip()
    nozzle = str(model.get("nozzle_diameter", "")).strip()
    if vendor != "Generic Printer" or not name or not nozzle:
        return None
    return f"{name} {nozzle} nozzle"


def _collect_strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, dict):
        out: list[str] = []
        for item in value.values():
            out.extend(_collect_strings(item))
        return tuple(out)
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(_collect_strings(item))
        return tuple(out)
    return ()


def _ensure_no_volatile_state(root: Path, payload: dict[str, Any]) -> None:
    forbidden_paths = [
        root / ".orcaslicer_machine_id",
        root / "log",
    ]
    forbidden_paths.extend(root.glob("user_backup*"))
    present = [path.name for path in forbidden_paths if path.exists()]
    if present:
        raise SlicerConfigError(
            "Orca datadir template contains runtime-only state: "
            + ", ".join(sorted(present))
        )

    if "recent" in payload or "recent_projects" in payload:
        raise SlicerConfigError(
            "Orca datadir template must not contain recent/recent_projects state."
        )

    app = payload.get("app")
    if isinstance(app, dict):
        bad_keys = sorted(_VOLATILE_APP_KEYS.intersection(app))
        if bad_keys:
            raise SlicerConfigError(
                "Orca datadir template contains volatile app keys: "
                + ", ".join(bad_keys)
            )

    absolute_values = [
        value for value in _collect_strings(payload) if _WINDOWS_ABSOLUTE_PATH_RE.search(value)
    ]
    if absolute_values:
        raise SlicerConfigError(
            "Orca datadir template contains machine-local absolute paths."
        )


def validate_orca_datadir_template(
    path: str | Path,
    *,
    expected_sha256: str | None = None,
    required_printer_profiles: tuple[str, ...] = DEFAULT_GENERIC_PRINTER_PRINTER_PROFILES,
    required_material_profiles: tuple[str, ...] = DEFAULT_GENERIC_PRINTER_MATERIAL_PROFILES,
    required_process_profiles: tuple[str, ...] = DEFAULT_GENERIC_PRINTER_PROCESS_PROFILES,
) -> OrcaDatadirTemplateIdentity:
    """Validate and fingerprint a clean Generic Printer-enabled Orca datadir template."""

    root = Path(path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise SlicerConfigError(
            f"ORCA_SUPERVISED_DATADIR_TEMPLATE must be an existing directory: {path!r}"
        )

    conf_path = root / _CONF_FILENAME
    if not conf_path.is_file():
        raise SlicerConfigError("Orca datadir template is missing OrcaSlicer.conf.")

    payload = _conf_payload(conf_path)
    _ensure_no_volatile_state(root, payload)

    generic_printer_root = root / "system" / "Generic Printer"
    generic_printer_json = root / "system" / "Generic Printer.json"
    if not generic_printer_root.is_dir() or not generic_printer_json.is_file():
        raise SlicerConfigError(
            "Orca datadir template must include system/Generic Printer and system/Generic Printer.json."
        )

    models = payload.get("models")
    if not isinstance(models, list):
        raise SlicerConfigError("OrcaSlicer.conf is missing Generic Printer-enabled models.")
    printer_profiles = tuple(
        sorted(
            name
            for item in models
            if isinstance(item, dict)
            for name in [_printer_profile_name(item)]
            if name is not None
        )
    )
    missing_printers = sorted(set(required_printer_profiles) - set(printer_profiles))
    if missing_printers:
        raise SlicerConfigError(
            "Orca datadir template is missing required printer profiles: "
            + ", ".join(missing_printers)
        )

    presets = payload.get("orca_presets")
    if not isinstance(presets, list):
        raise SlicerConfigError("OrcaSlicer.conf is missing orca_presets.")
    material_profiles = tuple(
        sorted(
            str(item.get("filament", "")).strip()
            for item in presets
            if isinstance(item, dict) and str(item.get("filament", "")).strip()
        )
    )
    process_profiles = tuple(
        sorted(
            str(item.get("process", "")).strip()
            for item in presets
            if isinstance(item, dict) and str(item.get("process", "")).strip()
        )
    )
    missing_materials = sorted(set(required_material_profiles) - set(material_profiles))
    if missing_materials:
        raise SlicerConfigError(
            "Orca datadir template is missing required material profiles: "
            + ", ".join(missing_materials)
        )
    missing_processes = sorted(set(required_process_profiles) - set(process_profiles))
    if missing_processes:
        raise SlicerConfigError(
            "Orca datadir template is missing required process profiles: "
            + ", ".join(missing_processes)
        )

    files = _iter_files(root)
    sha256, byte_count = _hash_datadir(root, files)
    if expected_sha256 and sha256.lower() != expected_sha256.lower():
        raise SlicerConfigError(
            "Orca datadir template SHA256 mismatch "
            f"(expected {expected_sha256}, actual {sha256})."
        )

    header = str(payload.get("header", ""))
    return OrcaDatadirTemplateIdentity(
        root=root,
        sha256=sha256,
        file_count=len(files),
        byte_count=byte_count,
        header=header,
        printer_profiles=printer_profiles,
        material_profiles=material_profiles,
        process_profiles=process_profiles,
    )


__all__ = [
    "DEFAULT_GENERIC_PRINTER_MATERIAL_PROFILES",
    "DEFAULT_GENERIC_PRINTER_PRINTER_PROFILES",
    "DEFAULT_GENERIC_PRINTER_PROCESS_PROFILES",
    "OrcaDatadirTemplateIdentity",
    "validate_orca_datadir_template",
]
