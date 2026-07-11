"""Sandbox runner contract for LLM-authored DSL execution (DESIGN.md section 4.3, R4)."""

import ast
import asyncio
import os
import shutil
import sys
import time
from abc import ABC, abstractmethod
from pathlib import Path

from modules.cad_mechanical.exceptions import (
    SandboxError,
    SandboxMemoryError,
    SandboxTimeoutError,
)
from modules.cad_mechanical.schemas import (
    CADDialect,
    GenerationRequest,
    SandboxExecutionResult,
)

_ALLOWED_IMPORT_ROOTS = frozenset({"cadquery", "build123d", "math", "json", "copy"})
# ``importlib`` and ``pkgutil`` calls can bypass the import allowlist via
# runtime module loading; block their known entry points as well.
_FORBIDDEN_CALL_NAMES = frozenset(
    {"eval", "exec", "compile", "__import__", "open", "input", "import_module", "find_module"}
)
_DEFAULT_DOCKER_IMAGE = "model_forge-cad-sandbox:2026.05.18"
_DEFAULT_SANDBOX_ROOT = Path(".model_forge/cad-sandbox")
_SANDBOX_SCRIPT_NAME = "_model_forge_cad_entry.py"

#: Public constant: the expected artifact stem that all sandbox runners and
#: CAD adapters must agree on.  The runner writes the script and looks for
#: ``<SANDBOX_ARTIFACT_STEM>.<format>``; the adapter appends an export call
#: that writes to the same name.
SANDBOX_ARTIFACT_STEM = "sandbox-result"
_SANDBOX_ARTIFACT_STEM = SANDBOX_ARTIFACT_STEM  # backward-compat alias

_ALLOW_HOST_EXEC_ENV = "CAD_SANDBOX_ALLOW_HOST_EXEC"


class SandboxRunner(ABC):
    """Executes an LLM-authored DSL script under the documented isolation.

    Implementations must enforce, at minimum:
        - the wall clock / memory / CPU quotas declared in ``request.limits``;
        - the import allowlist defined in `docs/decisions/ADR-0001-cad-sandbox.md`
          §"Sandbox Policy ADR" (cadquery / build123d / math / json / copy);
        - filesystem isolation: only ``request.output_dir`` is writable from
          inside the sandbox;
        - network isolation per ``request.limits.network_enabled``.

    The runner does not interpret the script; it only enforces boundaries
    and reports back what the script produced.
    """

    @property
    @abstractmethod
    def runner_name(self) -> str:
        """Stable identifier surfaced in logs and traces."""

    @abstractmethod
    async def execute(self, request: GenerationRequest) -> SandboxExecutionResult:
        """Run ``request.code`` inside the isolation boundary.

        Raises:
            SandboxError: the runner could not establish or maintain the
                isolation boundary (Docker unavailable, allowlist violation,
                quota breach). Topology / DSL-level failures are raised by
                the adapter, not the runner.
        """

    def health_check(self) -> bool:
        """Return False unless overridden. Concrete runners must probe Docker / CLI.

        Default is conservative (False) so any subclass that forgets to override
        is treated as unhealthy rather than silently accepted. For the sandbox
        runner this is the safe default: a broken runner boundary must fail
        loud, not pass silently.
        """
        return False


class RejectAllRunner(SandboxRunner):
    """Sandbox runner that refuses every execution request.

    Used as the default wiring while no isolation implementation exists,
    so accidental "just exec it in-process" code paths trip an explicit
    ``SandboxError`` instead of silently running untrusted LLM output
    against the host interpreter.
    """

    @property
    def runner_name(self) -> str:
        return "reject-all"

    async def execute(self, request: GenerationRequest) -> SandboxExecutionResult:
        raise SandboxError(
            "No sandbox runner is configured. Refusing to execute "
            f"{request.dialect.value} DSL on the host interpreter "
            "(DESIGN.md R4). Wire a Docker-backed SandboxRunner before "
            "running LLM-authored CAD code."
        )

    def health_check(self) -> bool:
        return False


class DockerSandboxRunner(SandboxRunner):
    """Docker-backed sandbox boundary for real CAD adapters."""

    def __init__(
        self,
        *,
        image: str = _DEFAULT_DOCKER_IMAGE,
        docker_binary: str = "docker",
        sandbox_root: Path | str = _DEFAULT_SANDBOX_ROOT,
    ) -> None:
        if not _is_pinned_image(image):
            raise SandboxError(
                "Docker sandbox image must be version-pinned with a non-latest tag "
                "or sha256 digest before executing LLM-authored CAD code."
            )
        self._image = image
        self._docker_binary = docker_binary
        self._sandbox_root = Path(sandbox_root).resolve()

    @property
    def runner_name(self) -> str:
        return "docker"

    def health_check(self) -> bool:
        return (
            shutil.which(self._docker_binary) is not None
            and self._sandbox_root.exists()
            and self._sandbox_root.is_dir()
        )

    async def execute(self, request: GenerationRequest) -> SandboxExecutionResult:
        if request.dialect is CADDialect.OPENSCAD:
            raise SandboxError(
                "DockerSandboxRunner currently supports Python CAD dialects only "
                "(cadquery/build123d). OpenSCAD needs a separate adapter command."
            )
        if shutil.which(self._docker_binary) is None:
            raise SandboxError(
                f"Docker binary '{self._docker_binary}' was not found. "
                "Install Docker or use RejectAllRunner for non-exec tests."
            )
        sandbox_root = _resolve_existing_dir(self._sandbox_root, "sandbox_root")
        output_dir = _resolve_existing_dir(request.output_dir, "output_dir")
        _validate_output_dir_under_sandbox_root(output_dir, sandbox_root)

        _preflight_code(request)

        script_path = output_dir / _SANDBOX_SCRIPT_NAME
        artifact_path = output_dir / f"{_SANDBOX_ARTIFACT_STEM}.{request.format}"
        artifact_path.unlink(missing_ok=True)
        script_path.write_text(request.code, encoding="utf-8")

        command = self._build_command(request, script_path, output_dir)
        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=request.limits.timeout_s,
            )
        except TimeoutError as exc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass  # already dead — still need to wait to reap zombie
            await proc.wait()
            raise SandboxTimeoutError(
                f"Docker sandbox exceeded {request.limits.timeout_s:.1f}s timeout."
            ) from exc
        finally:
            script_path.unlink(missing_ok=True)

        duration_s = time.monotonic() - start
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            if proc.returncode in {137, -9}:
                raise SandboxMemoryError(
                    "Docker sandbox was killed, likely by memory or process limits."
                )
            raise SandboxError(
                f"Docker sandbox failed with exit code {proc.returncode}: {stderr.strip()}"
            )
        _validate_artifact_path(
            artifact_path=artifact_path,
            output_dir=output_dir,
            expected_suffix=f".{request.format}",
            runner_name="Docker sandbox",
        )

        return SandboxExecutionResult(
            artifact_path=artifact_path,
            stdout=stdout,
            stderr=stderr,
            duration_s=duration_s,
            peak_memory_mb=0.0,
        )

    def _build_command(
        self,
        request: GenerationRequest,
        script_path: Path,
        output_dir: Path,
    ) -> list[str]:
        resolved_output_dir = str(output_dir)
        command = [
            self._docker_binary,
            "run",
            "--rm",
            "--memory",
            f"{request.limits.max_memory_mb}m",
            "--cpus",
            str(request.limits.max_cpu_cores),
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
        if not request.limits.network_enabled:
            command.extend(["--network", "none"])
        command.extend(
            [
                "-v",
                f"{resolved_output_dir}:/workspace/out:rw",
                "-w",
                "/workspace/out",
                self._image,
                "python",
                f"/workspace/out/{script_path.name}",
            ]
        )
        return command


class SubprocessSandboxRunner(SandboxRunner):
    """**LOCAL-DEV-ONLY** subprocess runner.  NOT for production or multi-tenant use.

    Runs LLM-authored code as a child Python process in ``output_dir`` with
    a wall-clock timeout but **without** Docker isolation, network blocking,
    memory caps, or per-PID limits.  The import-allowlist AST preflight from
    ADR-0001 is still enforced, but it is **not** a security boundary — it
    only rejects obviously dangerous patterns; the child process has full host
    filesystem access.

    This runner exists solely to allow local iteration on developer machines
    that do not have Docker available.  Any deployment that accepts untrusted
    prompts must use :class:`DockerSandboxRunner`.

    Requires ``CAD_SANDBOX_ALLOW_HOST_EXEC=true`` to be set in the environment
    at *both* factory creation time and at ``execute()`` call time (defense in
    depth).  The factory raises :class:`SandboxError` if the env var is absent;
    the runtime check in ``execute()`` is an extra failsafe.

    See ``docs/decisions/ADR-0001-cad-sandbox.md`` (amendment: Phase 9A
    subprocess runner) for rationale and risk acknowledgement.
    """

    LOCAL_DEV_ONLY: bool = True

    def __init__(
        self,
        *,
        sandbox_root: Path | str = _DEFAULT_SANDBOX_ROOT,
    ) -> None:
        self._sandbox_root = Path(sandbox_root).resolve()

    @property
    def runner_name(self) -> str:
        return "subprocess-local-dev"

    def health_check(self) -> bool:
        return (
            _is_host_exec_allowed()
            and self._sandbox_root.exists()
            and self._sandbox_root.is_dir()
        )

    async def execute(self, request: GenerationRequest) -> SandboxExecutionResult:
        if not _is_host_exec_allowed():
            raise SandboxError(
                f"SubprocessSandboxRunner requires {_ALLOW_HOST_EXEC_ENV}=true. "
                "This runner is LOCAL-DEV-ONLY and must never be enabled in "
                "production (see ADR-0001 amendment).  Set the env var explicitly "
                "to acknowledge this risk before running LLM-authored code."
            )

        sandbox_root = _resolve_existing_dir(self._sandbox_root, "sandbox_root")
        output_dir = _resolve_existing_dir(request.output_dir, "output_dir")
        _validate_output_dir_under_sandbox_root(output_dir, sandbox_root)

        _preflight_code(request)

        script_path = output_dir / _SANDBOX_SCRIPT_NAME
        artifact_path = output_dir / f"{SANDBOX_ARTIFACT_STEM}.{request.format}"
        artifact_path.unlink(missing_ok=True)
        script_path.write_text(request.code, encoding="utf-8")

        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(output_dir),
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=request.limits.timeout_s,
            )
        except TimeoutError as exc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            raise SandboxTimeoutError(
                f"Subprocess sandbox exceeded {request.limits.timeout_s:.1f}s timeout."
            ) from exc
        finally:
            script_path.unlink(missing_ok=True)

        duration_s = time.monotonic() - start
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            raise SandboxError(
                f"Subprocess sandbox failed with exit code {proc.returncode}: "
                f"{stderr.strip()}"
            )
        _validate_artifact_path(
            artifact_path=artifact_path,
            output_dir=output_dir,
            expected_suffix=f".{request.format}",
            runner_name="Subprocess sandbox",
        )

        return SandboxExecutionResult(
            artifact_path=artifact_path,
            stdout=stdout,
            stderr=stderr,
            duration_s=duration_s,
            peak_memory_mb=0.0,
        )


def _is_pinned_image(image: str) -> bool:
    if "@sha256:" in image:
        return True
    image_name = image.rsplit("/", 1)[-1]
    if ":" not in image_name:
        return False
    tag = image_name.rsplit(":", 1)[1]
    return bool(tag) and tag != "latest"


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


def _validate_artifact_path(
    *,
    artifact_path: Path,
    output_dir: Path,
    expected_suffix: str,
    runner_name: str,
) -> None:
    resolved_output = output_dir.resolve()
    resolved_artifact = artifact_path.resolve()
    if not resolved_artifact.is_relative_to(resolved_output):
        raise SandboxError(
            f"{runner_name} returned artifact outside output_dir: "
            f"{resolved_artifact}."
        )
    if resolved_artifact.suffix.lower() != expected_suffix:
        raise SandboxError(
            f"{runner_name} artifact must end with {expected_suffix!r}; "
            f"got {resolved_artifact.name!r}."
        )
    if not resolved_artifact.exists() or not resolved_artifact.is_file():
        raise SandboxError(
            f"{runner_name} completed but did not create expected artifact "
            f"'{artifact_path.name}'."
        )


def _preflight_code(request: GenerationRequest) -> None:
    if request.dialect not in {CADDialect.CADQUERY, CADDialect.BUILD123D}:
        return
    try:
        tree = ast.parse(request.code)
    except SyntaxError as exc:
        raise SandboxError(f"CAD script failed Python syntax preflight: {exc}") from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _validate_import_root(alias.name)
        elif isinstance(node, ast.ImportFrom):
            _validate_import_root(node.module or "")
        elif isinstance(node, ast.Call):
            call_name = _call_name(node.func)
            if call_name in _FORBIDDEN_CALL_NAMES:
                raise SandboxError(f"Forbidden call '{call_name}' in CAD script.")


def _validate_import_root(module_name: str) -> None:
    root = module_name.split(".", 1)[0]
    if root not in _ALLOWED_IMPORT_ROOTS:
        raise SandboxError(f"Import '{module_name}' is not allowed in CAD sandbox.")


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_host_exec_allowed() -> bool:
    """Return True only when the operator has explicitly set the opt-in env var."""
    return os.environ.get(_ALLOW_HOST_EXEC_ENV, "").lower() == "true"
