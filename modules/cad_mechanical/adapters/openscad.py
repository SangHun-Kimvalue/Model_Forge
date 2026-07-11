"""OpenSCAD mechanical CAD adapter.

Executes LLM-authored OpenSCAD source through the OpenSCAD CLI and exports a
single STL artifact named ``sandbox-result.stl`` inside ``request.output_dir``.

This adapter intentionally does not use ``SandboxRunner``: that runner is for
Python CAD dialects (CadQuery / build123d).  OpenSCAD has its own subprocess
or Docker boundary, while still sharing the same ``CAD_SANDBOX_ROOT``
output-dir invariant and artifact contract.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Protocol

from modules.cad_mechanical.base import BaseMechanicalCADGenerator
from modules.cad_mechanical.exceptions import (
    CadMechanicalConfigError,
    GenerationError,
    SandboxError,
    SandboxTimeoutError,
)
from modules.cad_mechanical.runner import SANDBOX_ARTIFACT_STEM, _is_pinned_image
from modules.cad_mechanical.schemas import (
    CADDialect,
    GenerationRequest,
    GenerationResult,
    SandboxLimits,
)

__all__ = ["OpenSCADAdapter", "OpenSCADDockerRunner"]

_ENTRY_SCRIPT_NAME = "_model_forge_openscad_entry.scad"
_EXTERNAL_FILE_ACCESS_RE = re.compile(
    r"(?im)^\s*(?:include|use)\s*<[^>]+>|\bimport\s*\(|\bsurface\s*\("
)
_METADATA_HEAD_CHARS = 500
_DEFAULT_LIMITS = SandboxLimits()
_LIMITS_ENFORCED = {
    "timeout_s": True,
    "max_memory_mb": False,
    "max_cpu_cores": False,
    "network_enabled": False,
}


class _CommandRunner(Protocol):
    @property
    def boundary_name(self) -> str: ...

    @property
    def sandboxed(self) -> bool: ...

    @property
    def requires_host_binary(self) -> bool: ...

    @property
    def limits_enforced(self) -> dict[str, bool]: ...

    def health_check(
        self,
        *,
        binary_path: str,
        sandbox_root: Path,
        expected_version: str | None,
    ) -> bool: ...

    async def run(
        self,
        command: list[str],
        *,
        cwd: Path,
        timeout_s: float,
        limits: SandboxLimits,
    ) -> tuple[int, str, str]: ...


class _DefaultOpenSCADRunner:
    @property
    def boundary_name(self) -> str:
        return "openscad-cli-subprocess"

    @property
    def sandboxed(self) -> bool:
        return False

    @property
    def requires_host_binary(self) -> bool:
        return True

    @property
    def limits_enforced(self) -> dict[str, bool]:
        return dict(_LIMITS_ENFORCED)

    def health_check(
        self,
        *,
        binary_path: str,
        sandbox_root: Path,
        expected_version: str | None,
    ) -> bool:
        resolved = _resolve_binary(binary_path)
        if resolved is None:
            return False
        if not sandbox_root.exists() or not sandbox_root.is_dir():
            return False
        if expected_version:
            try:
                completed = subprocess.run(
                    [resolved, "--version"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                )
            except (OSError, subprocess.TimeoutExpired):
                return False
            version_text = f"{completed.stdout}\n{completed.stderr}"
            return expected_version in version_text
        return True

    async def run(
        self,
        command: list[str],
        *,
        cwd: Path,
        timeout_s: float,
        limits: SandboxLimits,
    ) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
            )
        except OSError as exc:
            raise SandboxError(
                f"OpenSCAD CLI could not start binary {command[0]!r}: {exc}"
            ) from exc

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_s,
            )
        except TimeoutError as exc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            raise SandboxTimeoutError(
                f"OpenSCAD CLI exceeded {timeout_s:.1f}s timeout."
            ) from exc

        return (
            proc.returncode if proc.returncode is not None else 1,
            stdout_b.decode("utf-8", errors="replace"),
            stderr_b.decode("utf-8", errors="replace"),
        )


class OpenSCADDockerRunner:
    """Docker-backed OpenSCAD boundary for untrusted SCAD execution."""

    def __init__(
        self,
        *,
        image: str,
        docker_binary: str = "docker",
        container_binary: str = "openscad",
    ) -> None:
        if not _is_pinned_image(image):
            raise SandboxError(
                "OpenSCAD Docker image must be version-pinned with a non-latest "
                "tag or sha256 digest before executing untrusted SCAD."
            )
        self._image = image
        self._docker_binary = docker_binary
        self._container_binary = container_binary

    @property
    def boundary_name(self) -> str:
        return "openscad-docker"

    @property
    def sandboxed(self) -> bool:
        return True

    @property
    def requires_host_binary(self) -> bool:
        return False

    @property
    def limits_enforced(self) -> dict[str, bool]:
        return {
            "timeout_s": True,
            "max_memory_mb": True,
            "max_cpu_cores": True,
            "network_enabled": True,
        }

    def health_check(
        self,
        *,
        binary_path: str,
        sandbox_root: Path,
        expected_version: str | None,
    ) -> bool:
        if shutil.which(self._docker_binary) is None:
            return False
        if not sandbox_root.exists() or not sandbox_root.is_dir():
            return False
        if expected_version:
            try:
                completed = subprocess.run(
                    [
                        self._docker_binary,
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        self._image,
                        binary_path,
                        "--version",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=15.0,
                )
            except (OSError, subprocess.TimeoutExpired):
                return False
            version_text = f"{completed.stdout}\n{completed.stderr}"
            return completed.returncode == 0 and expected_version in version_text
        return True

    async def run(
        self,
        command: list[str],
        *,
        cwd: Path,
        timeout_s: float,
        limits: SandboxLimits,
    ) -> tuple[int, str, str]:
        artifact_name = command[2]
        entry_name = command[3]
        docker_command = self._build_command(
            artifact_name=artifact_name,
            entry_name=entry_name,
            cwd=cwd,
            limits=limits,
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *docker_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise SandboxError(
                f"OpenSCAD Docker boundary could not start {self._docker_binary!r}: "
                f"{exc}"
            ) from exc

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_s,
            )
        except TimeoutError as exc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            raise SandboxTimeoutError(
                f"OpenSCAD Docker boundary exceeded {timeout_s:.1f}s timeout."
            ) from exc

        return (
            proc.returncode if proc.returncode is not None else 1,
            stdout_b.decode("utf-8", errors="replace"),
            stderr_b.decode("utf-8", errors="replace"),
        )

    def _build_command(
        self,
        *,
        artifact_name: str,
        entry_name: str,
        cwd: Path,
        limits: SandboxLimits,
    ) -> list[str]:
        command = [
            self._docker_binary,
            "run",
            "--rm",
            "--memory",
            f"{limits.max_memory_mb}m",
            "--cpus",
            str(limits.max_cpu_cores),
            "--pids-limit",
            "128",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
        ]
        command.extend(["--network", "none"])
        command.extend(
            [
                "-v",
                f"{cwd}:/workspace/out:rw",
                "-w",
                "/workspace/out",
                self._image,
                self._container_binary,
                "-o",
                artifact_name,
                entry_name,
            ]
        )
        return command


class OpenSCADAdapter(BaseMechanicalCADGenerator):
    """Render OpenSCAD source to STL through the OpenSCAD CLI."""

    default_adapter_name = "openscad-v1"
    _SUPPORTED_FORMATS = frozenset({"stl"})

    def __init__(
        self,
        *,
        binary_path: str = "openscad",
        sandbox_root: Path | str,
        expected_version: str | None = None,
        runner: _CommandRunner | None = None,
        adapter_label: str | None = None,
    ) -> None:
        if not binary_path:
            raise CadMechanicalConfigError("OPENSCAD_BINARY_PATH must not be empty.")
        self._binary_path = binary_path
        self._sandbox_root = Path(sandbox_root).resolve()
        self._expected_version = expected_version
        self._runner: _CommandRunner = runner or _DefaultOpenSCADRunner()
        self._adapter_label = adapter_label or self.default_adapter_name

    @property
    def adapter_name(self) -> str:
        return self._adapter_label

    def health_check(self) -> bool:
        return self._runner.health_check(
            binary_path=self._binary_path,
            sandbox_root=self._sandbox_root,
            expected_version=self._expected_version,
        )

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        if request.dialect is not CADDialect.OPENSCAD:
            raise GenerationError(
                "OpenSCADAdapter supports only dialect 'openscad'; "
                f"got '{request.dialect.value}'."
            )
        if request.format not in self._SUPPORTED_FORMATS:
            raise GenerationError(
                f"OpenSCADAdapter supports {sorted(self._SUPPORTED_FORMATS)} "
                f"output formats; got '{request.format}'."
            )
        if _EXTERNAL_FILE_ACCESS_RE.search(request.code):
            raise SandboxError(
                "OpenSCAD external file directives (include/use/import/surface) "
                "are disabled in the first adapter version to prevent arbitrary "
                "file access."
            )
        _validate_supported_limits(request.limits, self._runner.limits_enforced)

        sandbox_root = _resolve_existing_dir(self._sandbox_root, "sandbox_root")
        output_dir = _resolve_existing_dir(request.output_dir, "output_dir")
        _validate_output_dir_under_sandbox_root(output_dir, sandbox_root)

        binary = self._binary_path
        if self._runner.requires_host_binary:
            resolved = _resolve_binary(self._binary_path)
            if resolved is None:
                raise CadMechanicalConfigError(
                    f"OpenSCAD binary {self._binary_path!r} was not found. "
                    "Set OPENSCAD_BINARY_PATH to a valid executable."
                )
            binary = resolved

        entry_path = output_dir / _ENTRY_SCRIPT_NAME
        artifact_path = output_dir / f"{SANDBOX_ARTIFACT_STEM}.stl"
        _remove_stale_file(entry_path, label="OpenSCAD entry script")
        _remove_stale_file(artifact_path, label="OpenSCAD artifact")
        entry_path.write_text(request.code, encoding="utf-8")

        command = [
            binary,
            "-o",
            artifact_path.name,
            entry_path.name,
        ]

        start = time.monotonic()
        rc, stdout, stderr = await self._runner.run(
            command,
            cwd=output_dir,
            timeout_s=request.limits.timeout_s,
            limits=request.limits,
        )
        elapsed = time.monotonic() - start

        if rc != 0:
            raise SandboxError(
                f"OpenSCAD CLI failed with exit code {rc}: {stderr.strip()}"
            )

        _validate_artifact(
            artifact_path=artifact_path,
            output_dir=output_dir,
            expected_suffix=".stl",
        )

        return GenerationResult(
            path=artifact_path,
            format=request.format,
            dialect=request.dialect,
            adapter_used=self._adapter_label,
            generation_time_s=elapsed,
            metadata={
                "sandboxed": self._runner.sandboxed,
                "boundary": self._runner.boundary_name,
                "runner": "openscad-cli",
                "binary_path": binary,
                "expected_version": self._expected_version,
                "limits_enforced": self._runner.limits_enforced,
                "stdout_head": stdout[:_METADATA_HEAD_CHARS],
                "stderr_head": stderr[:_METADATA_HEAD_CHARS],
            },
        )


def _resolve_binary(binary_path: str) -> str | None:
    path = Path(binary_path).expanduser()
    if path.parent != Path(".") or path.is_absolute():
        return str(path.resolve()) if path.is_file() else None
    return shutil.which(binary_path)


def _resolve_existing_dir(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise SandboxError(f"{label} '{path}' must exist and be a directory.")
    return resolved


def _validate_output_dir_under_sandbox_root(output_dir: Path, sandbox_root: Path) -> None:
    try:
        relative = output_dir.relative_to(sandbox_root)
    except ValueError as exc:
        raise SandboxError(
            f"output_dir '{output_dir}' must be under sandbox_root '{sandbox_root}'."
        ) from exc
    if relative == Path("."):
        raise SandboxError(
            f"output_dir '{output_dir}' must be a per-session child directory, "
            "not the sandbox_root itself."
        )


def _validate_supported_limits(
    limits: SandboxLimits,
    limits_enforced: dict[str, bool],
) -> None:
    unsupported: list[str] = []
    if (
        not limits_enforced["max_memory_mb"]
        and limits.max_memory_mb != _DEFAULT_LIMITS.max_memory_mb
    ):
        unsupported.append("max_memory_mb")
    if (
        not limits_enforced["max_cpu_cores"]
        and limits.max_cpu_cores != _DEFAULT_LIMITS.max_cpu_cores
    ):
        unsupported.append("max_cpu_cores")
    if (
        not limits_enforced["network_enabled"]
        and limits.network_enabled != _DEFAULT_LIMITS.network_enabled
    ):
        unsupported.append("network_enabled")
    if limits_enforced["network_enabled"] and limits.network_enabled:
        unsupported.append("network_enabled")
    if unsupported:
        raise SandboxError(
            "OpenSCADAdapter cannot satisfy the requested non-default limits "
            f"for this execution boundary: {', '.join(unsupported)}."
        )


def _remove_stale_file(path: Path, *, label: str) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        raise SandboxError(f"{label} path is a directory, not a file: {path}")
    try:
        path.unlink()
    except OSError as exc:
        raise SandboxError(f"Failed to remove stale {label}: {path}") from exc


def _validate_artifact(
    *,
    artifact_path: Path,
    output_dir: Path,
    expected_suffix: str,
) -> None:
    resolved_output = output_dir.resolve()
    resolved_artifact = artifact_path.resolve()
    if not resolved_artifact.is_relative_to(resolved_output):
        raise GenerationError(
            f"OpenSCAD artifact escaped output_dir: {resolved_artifact}."
        )
    if resolved_artifact.suffix.lower() != expected_suffix:
        raise GenerationError(
            f"OpenSCAD artifact must end with {expected_suffix!r}; "
            f"got {resolved_artifact.name!r}."
        )
    if not resolved_artifact.exists() or not resolved_artifact.is_file():
        raise GenerationError(
            f"OpenSCAD completed but did not create expected artifact "
            f"'{artifact_path.name}'."
        )
    if resolved_artifact.stat().st_size <= 0:
        raise GenerationError(
            f"OpenSCAD produced empty artifact '{artifact_path.name}'."
        )
