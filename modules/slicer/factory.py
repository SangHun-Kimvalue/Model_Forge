import logging
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from modules.observability.events import mock_selected_event, mock_selected_extra
from modules.slicer.adapters.mock import MockSlicer
from modules.slicer.adapters.orca_mcp import (
    DEFAULT_SLICE_TOOL_NAME,
    OrcaMCPSlicer,
)
from modules.slicer.adapters.orca_supervised import (
    DEFAULT_SUPERVISED_MCP_URL,
    OrcaSupervisedSlicer,
)
from modules.slicer.base import BaseSlicer
from modules.slicer.exceptions import SlicerConfigError
from modules.slicer.orca_datadir import validate_orca_datadir_template

AdapterName = Literal["mock", "orca_mcp", "orca_supervised", "orca_cli", "prusa", "bambu"]


class SlicerSettings(BaseModel):
    """Runtime settings for selecting a slicer adapter (DESIGN.md §4.5).

    Preconditions:
        ``adapter`` names the slicer the caller intends to use. The
        factory never silently downgrades an unsupported slicer to mock
        (DESIGN.md R10 — no silent fallback). Real adapters require a
        pinned binary version (R8).

    ``orca_mcp`` requires a External Runtime MCP-enabled binary running at
    ``orca_mcp_url`` (ADR-0003) plus an existing local binary path so
    runtime identity is explicit. ``orca_cli`` is a future standalone CLI
    subprocess adapter and still raises ``SlicerConfigError``.

    Raises:
        SlicerConfigError when the selected adapter has no concrete
        implementation in this build.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    adapter: AdapterName = Field(default="mock")
    adapter_label: str | None = Field(default=None)

    # orca_mcp settings (ADR-0003-orca-mcp-on-external_runtime-fork.md)
    orca_mcp_url: str = Field(default="http://127.0.0.1:13619/mcp")
    orca_mcp_binary_path: str | None = Field(default=None)
    orca_mcp_expected_version: str | None = Field(default=None)
    orca_mcp_slice_tool: str = Field(default=DEFAULT_SLICE_TOOL_NAME)
    orca_mcp_inter_slice_cooldown_s: float = Field(default=5.0)

    # orca_cli settings (future CLI subprocess adapter)
    orca_cli_binary_path: str | None = Field(default=None)

    # orca_supervised settings (ADR-0009 process-supervised adapter)
    orca_supervised_binary_path: str | None = Field(default=None)
    orca_supervised_expected_version: str | None = Field(default=None)
    orca_supervised_datadir_template: str | None = Field(default=None)
    orca_supervised_datadir_template_sha256: str | None = Field(default=None)
    orca_supervised_work_root: str = Field(default=".model_forge/runtime/orca-jobs")
    orca_supervised_startup_timeout_s: float = Field(default=60.0)
    orca_supervised_slice_timeout_s: float = Field(default=600.0)
    orca_supervised_shutdown_timeout_s: float = Field(default=15.0)
    orca_supervised_reuse_policy: Literal["per_job"] = Field(default="per_job")

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "SlicerSettings":
        source = environ if environ is not None else os.environ
        return cls(
            adapter=source.get("SLICER_ADAPTER", "mock"),
            adapter_label=source.get("SLICER_LABEL"),
            orca_mcp_url=source.get(
                "ORCA_MCP_URL", "http://127.0.0.1:13619/mcp"
            ),
            orca_mcp_binary_path=source.get("ORCA_MCP_BINARY_PATH") or None,
            orca_mcp_expected_version=source.get("ORCA_MCP_EXPECTED_VERSION") or None,
            orca_mcp_slice_tool=source.get(
                "ORCA_MCP_SLICE_TOOL", DEFAULT_SLICE_TOOL_NAME
            ),
            orca_mcp_inter_slice_cooldown_s=float(
                source.get("ORCA_MCP_INTER_SLICE_COOLDOWN_S", "5.0")
            ),
            orca_cli_binary_path=source.get("ORCA_CLI_BINARY_PATH") or None,
            orca_supervised_binary_path=source.get("ORCA_SUPERVISED_BINARY_PATH")
            or None,
            orca_supervised_expected_version=source.get(
                "ORCA_SUPERVISED_EXPECTED_VERSION"
            )
            or None,
            orca_supervised_datadir_template=source.get(
                "ORCA_SUPERVISED_DATADIR_TEMPLATE"
            )
            or None,
            orca_supervised_datadir_template_sha256=source.get(
                "ORCA_SUPERVISED_DATADIR_TEMPLATE_SHA256"
            )
            or None,
            orca_supervised_work_root=source.get(
                "ORCA_SUPERVISED_WORK_ROOT", ".model_forge/runtime/orca-jobs"
            ),
            orca_supervised_startup_timeout_s=float(
                source.get("ORCA_SUPERVISED_STARTUP_TIMEOUT_S", "60")
            ),
            orca_supervised_slice_timeout_s=float(
                source.get("ORCA_SUPERVISED_SLICE_TIMEOUT_S", "600")
            ),
            orca_supervised_shutdown_timeout_s=float(
                source.get("ORCA_SUPERVISED_SHUTDOWN_TIMEOUT_S", "15")
            ),
            orca_supervised_reuse_policy=source.get(
                "ORCA_SUPERVISED_REUSE_POLICY", "per_job"
            ),
        )


def create_slicer(
    settings: SlicerSettings | None = None,
    logger: logging.Logger | None = None,
) -> BaseSlicer:
    selected = settings or SlicerSettings.from_env()
    log = logger or logging.getLogger(__name__)

    if selected.adapter == "mock":
        label = selected.adapter_label or MockSlicer.default_adapter_name
        log.warning(
            mock_selected_event("slicer"),
            extra=mock_selected_extra(
                component="slicer",
                adapter="mock",
                label=label,
            ),
        )
        return MockSlicer(adapter_label=selected.adapter_label)

    if selected.adapter == "orca_mcp":
        if not selected.orca_mcp_binary_path or not selected.orca_mcp_expected_version:
            raise SlicerConfigError(
                "orca_mcp adapter requires both ORCA_MCP_BINARY_PATH and "
                "ORCA_MCP_EXPECTED_VERSION so the running External Runtime binary "
                f"is identified (current: binary={selected.orca_mcp_binary_path!r}, "
                f"version={selected.orca_mcp_expected_version!r}, "
                f"url={selected.orca_mcp_url!r}). See "
                "docs/decisions/ADR-0003-orca-mcp-on-external_runtime-fork.md "
                "and docs/HANDOFF.md for the Orca MCP merge session checklist. "
                "Set SLICER_ADAPTER=mock until the binary is pinned. "
                "No silent mock fallback (R10)."
            )
        binary_path = Path(selected.orca_mcp_binary_path).expanduser()
        if not binary_path.is_file():
            raise SlicerConfigError(
                "orca_mcp adapter requires ORCA_MCP_BINARY_PATH to point to "
                f"an existing binary file (current: {selected.orca_mcp_binary_path!r}). "
                "No silent mock fallback (R10)."
            )
        log.info(
            "slicer.orca_mcp_selected",
            extra={
                "component": "slicer",
                "adapter": "orca_mcp",
                "mcp_url": selected.orca_mcp_url,
                "binary_path": str(binary_path),
                "expected_version": selected.orca_mcp_expected_version,
                "tool_name": selected.orca_mcp_slice_tool,
                "inter_slice_cooldown_s": selected.orca_mcp_inter_slice_cooldown_s,
            },
        )
        return OrcaMCPSlicer(
            mcp_url=selected.orca_mcp_url,
            binary_path=str(binary_path),
            expected_version=selected.orca_mcp_expected_version,
            tool_name=selected.orca_mcp_slice_tool,
            inter_slice_cooldown_s=selected.orca_mcp_inter_slice_cooldown_s,
            adapter_label=selected.adapter_label,
        )

    if selected.adapter == "orca_cli":
        raise SlicerConfigError(
            "orca_cli adapter requires a pinned OrcaSlicer CLI binary "
            f"(ORCA_CLI_BINARY_PATH={selected.orca_cli_binary_path!r}). "
            "No concrete CLI subprocess adapter is implemented yet. "
            "Set SLICER_ADAPTER=mock until orca_cli is implemented."
        )

    if selected.adapter == "orca_supervised":
        if (
            not selected.orca_supervised_binary_path
            or not selected.orca_supervised_expected_version
            or not selected.orca_supervised_datadir_template
        ):
            raise SlicerConfigError(
                "orca_supervised adapter requires ORCA_SUPERVISED_BINARY_PATH, "
                "ORCA_SUPERVISED_EXPECTED_VERSION, and "
                "ORCA_SUPERVISED_DATADIR_TEMPLATE. No silent mock fallback (R10)."
            )
        binary_path = Path(selected.orca_supervised_binary_path).expanduser()
        if not binary_path.is_file():
            raise SlicerConfigError(
                "orca_supervised adapter requires ORCA_SUPERVISED_BINARY_PATH "
                f"to point to an existing binary file (current: "
                f"{selected.orca_supervised_binary_path!r})."
            )
        identity = validate_orca_datadir_template(
            selected.orca_supervised_datadir_template,
            expected_sha256=selected.orca_supervised_datadir_template_sha256,
        )
        log.info(
            "slicer.orca_supervised_selected",
            extra={
                "component": "slicer",
                "adapter": "orca_supervised",
                "mcp_url": DEFAULT_SUPERVISED_MCP_URL,
                "binary_path": str(binary_path),
                "expected_version": selected.orca_supervised_expected_version,
                "datadir_template": str(identity.root),
                "datadir_template_sha256": identity.sha256,
                "work_root": selected.orca_supervised_work_root,
                "startup_timeout_s": selected.orca_supervised_startup_timeout_s,
                "slice_timeout_s": selected.orca_supervised_slice_timeout_s,
                "shutdown_timeout_s": selected.orca_supervised_shutdown_timeout_s,
                "reuse_policy": selected.orca_supervised_reuse_policy,
            },
        )
        return OrcaSupervisedSlicer(
            binary_path=binary_path,
            expected_version=selected.orca_supervised_expected_version,
            datadir_template=identity,
            work_root=selected.orca_supervised_work_root,
            startup_timeout_s=selected.orca_supervised_startup_timeout_s,
            slice_timeout_s=selected.orca_supervised_slice_timeout_s,
            shutdown_timeout_s=selected.orca_supervised_shutdown_timeout_s,
            adapter_label=selected.adapter_label,
        )

    raise SlicerConfigError(
        f"Slicer adapter '{selected.adapter}' is selected but no concrete "
        "adapter is wired yet. Set SLICER_ADAPTER=mock for deterministic "
        "tests, or implement the adapter (with pinned binary version, R8) "
        "first. See docs/lessons/slicer.md."
    )


__all__ = ["SlicerSettings", "create_slicer"]
