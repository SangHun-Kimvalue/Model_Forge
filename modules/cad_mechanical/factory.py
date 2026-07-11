import logging
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from modules.cad_mechanical.adapters.mock import MockMechanicalCADGenerator
from modules.cad_mechanical.base import BaseMechanicalCADGenerator
from modules.cad_mechanical.exceptions import CadMechanicalConfigError
from modules.cad_mechanical.runner import (
    DockerSandboxRunner,
    RejectAllRunner,
    SubprocessSandboxRunner,
    _is_host_exec_allowed,
)
from modules.observability.events import mock_selected_event, mock_selected_extra

AdapterName = Literal["mock", "cadquery", "build123d", "openscad"]
RunnerName = Literal["reject_all", "docker", "subprocess"]
OpenSCADBoundaryName = Literal["subprocess", "docker"]


class CadMechanicalSettings(BaseModel):
    """Runtime settings for selecting a mechanical CAD adapter.

    Preconditions:
        ``adapter`` names the adapter the caller actually intends to use.
        The factory never silently downgrades an unsupported adapter to
        mock (DESIGN.md R10 — no silent fallback).

    Sandbox runner settings (used when ``adapter="cadquery"``):
        ``sandbox_runner``: which runner to use.
            - ``reject_all`` (default): refuses every execution request.
            - ``docker``: DockerSandboxRunner (requires Docker + pinned image).
            - ``subprocess``: SubprocessSandboxRunner (LOCAL-DEV-ONLY;
                requires ``CAD_SANDBOX_ALLOW_HOST_EXEC=true``).
        ``sandbox_root``: shared root directory for per-job output dirs.
        ``sandbox_docker_image``: pinned Docker image tag (docker runner only).
        ``sandbox_allow_host_exec``: must be True for subprocess runner.

    OpenSCAD settings (used when ``adapter="openscad"``):
        ``openscad_execution_boundary``: ``subprocess`` for local single-user
            runs or ``docker`` for untrusted/product SCAD execution.
        ``openscad_binary_path``: OpenSCAD CLI executable or absolute path.
        ``openscad_expected_version``: optional substring expected in
            ``openscad --version`` output.

    Raises:
        CadMechanicalConfigError when the selected adapter has no concrete
        implementation in this build, or when sandbox runner config is invalid.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    adapter: AdapterName = Field(default="mock")
    adapter_label: str | None = Field(default=None)

    # Sandbox runner settings (only relevant when adapter="cadquery")
    sandbox_runner: RunnerName = Field(default="reject_all")
    sandbox_root: str = Field(default=".model_forge/cad-sandbox")
    sandbox_docker_image: str = Field(default="model_forge-cad-sandbox:2026.05.18")
    sandbox_allow_host_exec: bool = Field(default=False)
    openscad_execution_boundary: OpenSCADBoundaryName = Field(default="subprocess")
    openscad_binary_path: str = Field(default="openscad")
    openscad_expected_version: str | None = Field(default=None)
    openscad_docker_image: str = Field(default="model_forge-openscad-sandbox:2026.05.19")
    openscad_docker_binary: str = Field(default="docker")

    @classmethod
    def from_env(
        cls, environ: dict[str, str] | None = None
    ) -> "CadMechanicalSettings":
        source = environ if environ is not None else os.environ
        return cls(
            adapter=source.get("CAD_MECHANICAL_ADAPTER", "mock"),
            adapter_label=source.get("CAD_MECHANICAL_LABEL"),
            sandbox_runner=source.get("CAD_SANDBOX_RUNNER", "reject_all"),
            sandbox_root=source.get("CAD_SANDBOX_ROOT", ".model_forge/cad-sandbox"),
            sandbox_docker_image=source.get(
                "CAD_SANDBOX_DOCKER_IMAGE", "model_forge-cad-sandbox:2026.05.18"
            ),
            sandbox_allow_host_exec=(
                source.get("CAD_SANDBOX_ALLOW_HOST_EXEC", "false").lower() == "true"
            ),
            openscad_binary_path=source.get("OPENSCAD_BINARY_PATH", "openscad"),
            openscad_expected_version=source.get("OPENSCAD_EXPECTED_VERSION") or None,
            openscad_execution_boundary=source.get(
                "OPENSCAD_EXECUTION_BOUNDARY", "subprocess"
            ),
            openscad_docker_image=source.get(
                "OPENSCAD_DOCKER_IMAGE", "model_forge-openscad-sandbox:2026.05.19"
            ),
            openscad_docker_binary=source.get("OPENSCAD_DOCKER_BINARY", "docker"),
        )


def create_mechanical_cad_generator(
    settings: CadMechanicalSettings | None = None,
    logger: logging.Logger | None = None,
) -> BaseMechanicalCADGenerator:
    selected = settings or CadMechanicalSettings.from_env()
    log = logger or logging.getLogger(__name__)

    if selected.adapter == "mock":
        label = selected.adapter_label or MockMechanicalCADGenerator.default_adapter_name
        log.warning(
            mock_selected_event("cad_mechanical"),
            extra=mock_selected_extra(
                component="cad_mechanical",
                adapter="mock",
                label=label,
            ),
        )
        return MockMechanicalCADGenerator(adapter_label=selected.adapter_label)

    if selected.adapter == "cadquery":
        return _create_cadquery_adapter(selected, log)

    if selected.adapter == "openscad":
        return _create_openscad_adapter(selected)

    raise CadMechanicalConfigError(
        f"Mechanical CAD adapter '{selected.adapter}' is selected but no "
        "concrete adapter exists yet. Set CAD_MECHANICAL_ADAPTER=mock for "
        "deterministic tests, or use 'cadquery' with a configured runner "
        "or 'openscad' with OPENSCAD_BINARY_PATH "
        "(see docs/decisions/ADR-0001-cad-sandbox.md)."
    )


def _create_cadquery_adapter(
    settings: CadMechanicalSettings,
    log: logging.Logger,
) -> BaseMechanicalCADGenerator:
    from modules.cad_mechanical.adapters.cadquery import CadQueryAdapter

    runner = _create_runner(settings, log)
    return CadQueryAdapter(runner=runner, adapter_label=settings.adapter_label)


def _create_openscad_adapter(
    settings: CadMechanicalSettings,
) -> BaseMechanicalCADGenerator:
    from modules.cad_mechanical.adapters.openscad import (
        OpenSCADAdapter,
        OpenSCADDockerRunner,
    )

    runner = None
    if settings.openscad_execution_boundary == "docker":
        runner = OpenSCADDockerRunner(
            image=settings.openscad_docker_image,
            docker_binary=settings.openscad_docker_binary,
            container_binary=settings.openscad_binary_path,
        )

    adapter = OpenSCADAdapter(
        binary_path=settings.openscad_binary_path,
        sandbox_root=settings.sandbox_root,
        expected_version=settings.openscad_expected_version,
        runner=runner,
        adapter_label=settings.adapter_label,
    )
    if not adapter.health_check():
        version_hint = (
            f" and OPENSCAD_EXPECTED_VERSION={settings.openscad_expected_version!r}"
            if settings.openscad_expected_version
            else ""
        )
        raise CadMechanicalConfigError(
            "CAD_MECHANICAL_ADAPTER=openscad selected, but OpenSCADAdapter "
            "health_check() failed. Ensure CAD_SANDBOX_ROOT exists, "
            f"OPENSCAD_EXECUTION_BOUNDARY={settings.openscad_execution_boundary!r} "
            "is available, and the selected OpenSCAD runtime "
            f"{settings.openscad_binary_path!r} is executable{version_hint}. "
            "No mock fallback."
        )
    return adapter


def _create_runner(
    settings: CadMechanicalSettings,
    log: logging.Logger,
) -> "DockerSandboxRunner | SubprocessSandboxRunner | RejectAllRunner":
    sandbox_root = Path(settings.sandbox_root)

    if settings.sandbox_runner == "reject_all":
        log.warning(
            "CAD sandbox runner is 'reject_all' — CadQuery execution will fail. "
            "Set CAD_SANDBOX_RUNNER=docker or CAD_SANDBOX_RUNNER=subprocess "
            "(local-dev-only with CAD_SANDBOX_ALLOW_HOST_EXEC=true)."
        )
        return RejectAllRunner()

    if settings.sandbox_runner == "docker":
        runner = DockerSandboxRunner(
            image=settings.sandbox_docker_image,
            sandbox_root=sandbox_root,
        )
        if not runner.health_check():
            raise CadMechanicalConfigError(
                "CAD_SANDBOX_RUNNER=docker selected, but DockerSandboxRunner "
                "health_check() failed. Ensure Docker is installed, the "
                "configured CAD_SANDBOX_ROOT exists, and the image is pinned "
                "and available before enabling real CAD execution."
            )
        return runner

    if settings.sandbox_runner == "subprocess":
        if not settings.sandbox_allow_host_exec or not _is_host_exec_allowed():
            raise CadMechanicalConfigError(
                "SubprocessSandboxRunner requires CAD_SANDBOX_ALLOW_HOST_EXEC=true. "
                "This runner is LOCAL-DEV-ONLY (see ADR-0001 amendment). "
                "Set the env var explicitly to acknowledge this risk."
            )
        log.warning(
            "CAD sandbox runner is 'subprocess' (LOCAL-DEV-ONLY). "
            "Do not use in production or any multi-tenant environment."
        )
        return SubprocessSandboxRunner(sandbox_root=sandbox_root)

    raise CadMechanicalConfigError(
        f"Unknown sandbox runner '{settings.sandbox_runner}'. "
        "Valid values: reject_all, docker, subprocess."
    )
