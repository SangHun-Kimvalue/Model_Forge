"""Process-supervised Orca slicer adapter (ADR-0009).

``orca_mcp`` talks to a long-lived Orca GUI/MCP process.  Phase 9C-Live
proved that focused single slices can pass, but repeated slices in the
same process can hang.  This adapter keeps the existing MCP product tool
contract while moving the runtime boundary to one disposable Orca process
per slice job.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import shutil
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Protocol
from urllib.parse import urlparse

from modules.slicer.adapters.orca_mcp import DEFAULT_SLICE_TOOL_NAME, OrcaMCPSlicer
from modules.slicer.base import BaseSlicer
from modules.slicer.exceptions import SlicerConfigError, SlicerExecutionError
from modules.slicer.orca_datadir import OrcaDatadirTemplateIdentity
from modules.slicer.schemas import SliceRequest, SliceResult

__all__ = [
    "DEFAULT_SUPERVISED_MCP_URL",
    "OrcaSupervisedSlicer",
    "SupervisedOrcaJob",
]

DEFAULT_SUPERVISED_MCP_URL = "http://127.0.0.1:13619/mcp"


class _ProcessHandle(Protocol):
    @property
    def pid(self) -> int: ...

    @property
    def returncode(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


class _ProcessSupervisor(Protocol):
    async def start(
        self,
        cmd: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        stdout_path: Path,
        stderr_path: Path,
    ) -> _ProcessHandle: ...

    async def stop(self, process: _ProcessHandle, *, timeout_s: float) -> None: ...


class _ReadinessProbe(Protocol):
    async def wait_until_ready(
        self,
        *,
        mcp_url: str,
        expected_version: str,
        timeout_s: float,
    ) -> None: ...


class _MCPPortGuard(Protocol):
    async def assert_available(self, *, mcp_url: str) -> None: ...


@dataclass(frozen=True)
class SupervisedOrcaJob:
    """Job-local paths materialized from a clean Orca datadir template."""

    job_id: str
    root: Path
    datadir: Path
    stdout_path: Path
    stderr_path: Path
    failure_path: Path


class _DefaultProcessSupervisor:
    async def start(
        self,
        cmd: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        stdout_path: Path,
        stderr_path: Path,
    ) -> _ProcessHandle:
        try:
            stdout = stdout_path.open("wb")
            stderr = stderr_path.open("wb")
        except OSError as exc:
            raise SlicerExecutionError(
                f"Unable to open Orca supervised log files: {exc}"
            ) from exc
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(cwd),
                env=env,
                stdout=stdout,
                stderr=stderr,
            )
        except OSError as exc:
            stdout.close()
            stderr.close()
            raise SlicerExecutionError(
                f"Unable to start supervised Orca process {cmd[0]!r}: {exc}"
            ) from exc
        stdout.close()
        stderr.close()
        return proc

    async def stop(self, process: _ProcessHandle, *, timeout_s: float) -> None:
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout_s)
            return
        except TimeoutError:
            pass
        await self._kill_process_tree(process)

    async def _kill_process_tree(self, process: _ProcessHandle) -> None:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
            return
        try:
            process.kill()
        except ProcessLookupError:
            return
        await process.wait()


class _DefaultReadinessProbe:
    async def wait_until_ready(
        self,
        *,
        mcp_url: str,
        expected_version: str,
        timeout_s: float,
    ) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - factory-level dependency
            raise SlicerConfigError(
                "httpx is not installed. Install orchestrator/web dependencies "
                "before selecting SLICER_ADAPTER=orca_supervised."
            ) from exc

        deadline = time.monotonic() + timeout_s
        last_error: Exception | None = None
        request_id = 1
        async with httpx.AsyncClient(timeout=2.0) as client:
            while time.monotonic() < deadline:
                try:
                    response = await client.post(
                        mcp_url,
                        json={
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": "initialize",
                            "params": {
                                "client": "model_forge-orca-supervised",
                                "expected_version": expected_version,
                            },
                        },
                        headers={"Content-Type": "application/json"},
                    )
                    if response.status_code == 200:
                        payload = response.json()
                        result = payload.get("result")
                        if isinstance(result, dict):
                            server_info = result.get("serverInfo")
                            if not isinstance(server_info, dict):
                                server_info = result.get("server_info")
                            reported = (
                                server_info.get("version")
                                if isinstance(server_info, dict)
                                else result.get("version")
                            )
                            if reported == expected_version:
                                return
                            if isinstance(reported, str) and reported:
                                raise SlicerExecutionError(
                                    "Supervised Orca MCP version mismatch "
                                    f"(expected {expected_version!r}, got "
                                    f"{reported!r})."
                                )
                except Exception as exc:  # noqa: BLE001 - retry until deadline
                    last_error = exc
                request_id += 1
                await asyncio.sleep(0.5)
        raise SlicerExecutionError(
            "Supervised Orca MCP endpoint did not become ready within "
            f"{timeout_s:.0f}s at {mcp_url}. Last error: {last_error!r}"
        )


class _DefaultMCPPortGuard:
    def __init__(self, *, timeout_s: float = 0.5) -> None:
        self._timeout_s = timeout_s

    async def assert_available(self, *, mcp_url: str) -> None:
        host, port = self._host_port_from_url(mcp_url)
        try:
            await asyncio.to_thread(
                self._try_connect,
                host,
                port,
                self._timeout_s,
            )
        except ConnectionRefusedError:
            return
        except TimeoutError:
            return
        except OSError as exc:
            if exc.errno in {
                errno.ECONNREFUSED,
                errno.ETIMEDOUT,
                errno.ENETUNREACH,
                errno.EHOSTUNREACH,
            }:
                return
            raise SlicerConfigError(
                "Unable to check supervised Orca MCP port availability for "
                f"{mcp_url}: {exc}"
            ) from exc
        raise SlicerExecutionError(
            "Supervised Orca MCP endpoint is already occupied before process "
            f"startup: {mcp_url}. Stop the existing Orca MCP process or use a "
            "job-unique MCP port before selecting SLICER_ADAPTER=orca_supervised."
        )

    def _host_port_from_url(self, mcp_url: str) -> tuple[str, int]:
        parsed = urlparse(mcp_url)
        if not parsed.hostname:
            raise SlicerConfigError(
                f"orca_supervised mcp_url must include a hostname: {mcp_url!r}"
            )
        if parsed.scheme not in {"http", "https"}:
            raise SlicerConfigError(
                "orca_supervised mcp_url must use http or https with an explicit "
                f"port for supervised port ownership: {mcp_url!r}"
            )
        if parsed.port is None:
            raise SlicerConfigError(
                "orca_supervised mcp_url must include an explicit port for "
                f"supervised port ownership: {mcp_url!r}"
            )
        port = parsed.port
        return parsed.hostname, port

    def _try_connect(self, host: str, port: int, timeout_s: float) -> None:
        with socket.create_connection((host, port), timeout=timeout_s):
            return


_SlicerFactory = Callable[..., BaseSlicer]


class OrcaSupervisedSlicer(BaseSlicer):
    """Starts one fresh Orca GUI/MCP process per slicing job."""

    default_adapter_name = "orca-supervised"
    _host_lock: ClassVar[asyncio.Lock] = asyncio.Lock()

    def __init__(
        self,
        *,
        binary_path: str | Path,
        expected_version: str,
        datadir_template: OrcaDatadirTemplateIdentity,
        work_root: str | Path,
        mcp_url: str = DEFAULT_SUPERVISED_MCP_URL,
        startup_timeout_s: float = 60.0,
        slice_timeout_s: float = 600.0,
        shutdown_timeout_s: float = 15.0,
        adapter_label: str | None = None,
        process_supervisor: _ProcessSupervisor | None = None,
        readiness_probe: _ReadinessProbe | None = None,
        port_guard: _MCPPortGuard | None = None,
        slicer_factory: _SlicerFactory | None = None,
    ) -> None:
        self._binary_path = Path(binary_path).expanduser().resolve()
        if not self._binary_path.is_file():
            raise SlicerConfigError(
                f"ORCA_SUPERVISED_BINARY_PATH must be an existing file: {binary_path!r}"
            )
        if not expected_version:
            raise SlicerConfigError("ORCA_SUPERVISED_EXPECTED_VERSION is required.")
        if startup_timeout_s <= 0 or slice_timeout_s <= 0 or shutdown_timeout_s <= 0:
            raise SlicerConfigError(
                "Orca supervised timeouts must be positive numbers."
            )
        if not mcp_url:
            raise SlicerConfigError("orca_supervised mcp_url must be non-empty.")

        self._expected_version = expected_version
        self._datadir_template = datadir_template
        self._work_root = Path(work_root).expanduser().resolve()
        self._mcp_url = mcp_url
        self._startup_timeout_s = startup_timeout_s
        self._slice_timeout_s = slice_timeout_s
        self._shutdown_timeout_s = shutdown_timeout_s
        self._adapter_label = adapter_label or self.default_adapter_name
        self._process_supervisor = process_supervisor or _DefaultProcessSupervisor()
        self._readiness_probe = readiness_probe or _DefaultReadinessProbe()
        self._port_guard = port_guard or _DefaultMCPPortGuard()
        self._slicer_factory = slicer_factory or self._default_slicer_factory

    @property
    def adapter_name(self) -> str:
        return self._adapter_label

    @property
    def slicer_version(self) -> str:
        return self._expected_version

    def health_check(self) -> bool:
        return (
            self._binary_path.is_file()
            and self._datadir_template.root.is_dir()
            and bool(self._expected_version)
        )

    async def slice(self, request: SliceRequest) -> SliceResult:
        if not request.output_dir.exists() or not request.output_dir.is_dir():
            raise SlicerConfigError(
                f"output_dir '{request.output_dir}' is missing or not a directory."
            )
        if not request.mesh_path.exists() or not request.mesh_path.is_file():
            raise SlicerConfigError(
                f"mesh_path '{request.mesh_path}' is missing or not a file."
            )

        async with self._host_lock:
            job = self._materialize_job(request.session_id)
            process: _ProcessHandle | None = None
            stage = "startup"
            start = time.perf_counter()
            try:
                await self._port_guard.assert_available(mcp_url=self._mcp_url)
                process = await self._start_process(job)
                await self._readiness_probe.wait_until_ready(
                    mcp_url=self._mcp_url,
                    expected_version=self._expected_version,
                    timeout_s=self._startup_timeout_s,
                )
                stage = "slice"
                slicer = self._slicer_factory(
                    mcp_url=self._mcp_url,
                    binary_path=str(self._binary_path),
                    expected_version=self._expected_version,
                    adapter_label=self._adapter_label,
                )
                try:
                    result = await asyncio.wait_for(
                        slicer.slice(request), timeout=self._slice_timeout_s
                    )
                finally:
                    await slicer.close()
                self._validate_result_artifacts(result, request.output_dir)
                return self._with_supervised_metadata(
                    result=result,
                    job=job,
                    elapsed_s=time.perf_counter() - start,
                )
            except Exception as exc:
                self._write_failure_diagnostic(job, stage=stage, exc=exc)
                if isinstance(exc, (SlicerConfigError, SlicerExecutionError)):
                    raise
                raise SlicerExecutionError(
                    f"Supervised Orca slice failed: {exc}"
                ) from exc
            finally:
                if process is not None:
                    await self._process_supervisor.stop(
                        process, timeout_s=self._shutdown_timeout_s
                    )

    def _default_slicer_factory(self, **kwargs: Any) -> BaseSlicer:
        return OrcaMCPSlicer(
            **kwargs,
            tool_name=DEFAULT_SLICE_TOOL_NAME,
            timeout_s=self._slice_timeout_s,
            reset_session_between_slices=False,
        )

    def _materialize_job(self, session_id: str) -> SupervisedOrcaJob:
        safe_session = "".join(
            ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in session_id
        )
        job_id = f"{safe_session}-{uuid.uuid4().hex[:12]}"
        root = self._work_root / job_id
        datadir = root / "datadir"
        try:
            root.mkdir(parents=True, exist_ok=False)
            shutil.copytree(self._datadir_template.root, datadir)
        except OSError as exc:
            raise SlicerExecutionError(
                f"Unable to materialize supervised Orca datadir: {exc}"
            ) from exc
        return SupervisedOrcaJob(
            job_id=job_id,
            root=root,
            datadir=datadir,
            stdout_path=root / "orca-stdout.log",
            stderr_path=root / "orca-stderr.log",
            failure_path=root / "failure.json",
        )

    async def _start_process(self, job: SupervisedOrcaJob) -> _ProcessHandle:
        cmd = [
            str(self._binary_path),
            "--datadir",
            str(job.datadir),
        ]
        env = dict(os.environ)
        env["MODEL_FORGE_ORCA_SUPERVISED_JOB_ID"] = job.job_id
        env["MODEL_FORGE_ORCA_SUPERVISED_JOB_ROOT"] = str(job.root)
        return await self._process_supervisor.start(
            cmd,
            cwd=self._binary_path.parent,
            env=env,
            stdout_path=job.stdout_path,
            stderr_path=job.stderr_path,
        )

    def _validate_result_artifacts(self, result: SliceResult, output_dir: Path) -> None:
        output_root = output_dir.resolve()
        self._validate_artifact_path(result.gcode_path, output_root, ".gcode")
        if result.threemf_path is not None:
            self._validate_artifact_path(result.threemf_path, output_root, ".3mf")

    def _validate_artifact_path(
        self, path: Path, output_root: Path, expected_suffix: str
    ) -> None:
        resolved = path.expanduser().resolve()
        try:
            resolved.relative_to(output_root)
        except ValueError as exc:
            raise SlicerExecutionError(
                f"Supervised Orca returned artifact outside output_dir: {resolved}"
            ) from exc
        if resolved.suffix.lower() != expected_suffix:
            raise SlicerExecutionError(
                f"Supervised Orca returned artifact with wrong extension: {resolved}"
            )
        if not resolved.is_file():
            raise SlicerExecutionError(
                f"Supervised Orca returned missing artifact: {resolved}"
            )
        if resolved.stat().st_size <= 0:
            raise SlicerExecutionError(
                f"Supervised Orca returned empty artifact: {resolved}"
            )

    def _with_supervised_metadata(
        self,
        *,
        result: SliceResult,
        job: SupervisedOrcaJob,
        elapsed_s: float,
    ) -> SliceResult:
        metadata = dict(result.metadata)
        metadata.update(
            {
                "provider": "orca_supervised",
                "mcp_url": self._mcp_url,
                "binary_path": str(self._binary_path),
                "expected_version": self._expected_version,
                "datadir_template_sha256": self._datadir_template.sha256,
                "job_id": job.job_id,
                "job_root": str(job.root),
                "job_datadir": str(job.datadir),
                "stdout_path": str(job.stdout_path),
                "stderr_path": str(job.stderr_path),
                "supervised_elapsed_s": elapsed_s,
                "reuse_policy": "per_job",
            }
        )
        return result.model_copy(
            update={
                "adapter_used": self._adapter_label,
                "metadata": metadata,
            }
        )

    def _write_failure_diagnostic(
        self, job: SupervisedOrcaJob, *, stage: str, exc: BaseException
    ) -> None:
        payload = {
            "stage": stage,
            "error_type": type(exc).__name__,
            "detail": str(exc),
            "job_id": job.job_id,
            "job_root": str(job.root),
            "datadir_template_sha256": self._datadir_template.sha256,
        }
        try:
            job.failure_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass
