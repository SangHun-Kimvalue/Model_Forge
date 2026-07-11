"""Orca MCP slicer adapter (Phase 8B-2 / Phase 9C/9D).

Default product flow:

  1. ``initialize``          — JSON-RPC version assertion (ADR-0003 R8)
  2. ``model_forge_slice``     — Orca runtime loads the mesh, profiles, settings,
                              slices, exports 3MF, creates G-code, and returns
                              artifact paths plus stats.

Legacy diagnostic flow:

  If ``ORCA_MCP_SLICE_TOOL`` is explicitly set to ``slice_and_stats`` (or any
  non-default lower-level tool), this adapter keeps the old hybrid MCP+CLI path
  for debugging: ``model_load_file`` → profiles/settings → ``slice_and_stats`` →
  ``slice_status`` → ``model_export(3mf)`` → CLI ``--slice 0 --outputdir``.

Hard constraints (DESIGN.md R8, R10):
- ``binary_path`` and ``expected_version`` must be set. Missing config →
  ``SlicerConfigError``. The factory MUST NOT fall back to mock.
- The first ``slice()`` call performs an ``initialize`` JSON-RPC and
  asserts the reported version equals ``expected_version``. Mismatch →
  ``SlicerVersionError``.
- ``health_check()`` is synchronous (matches ``BaseSlicer``) and only
  validates configuration; runtime liveness is checked on the first
  ``slice()`` call so health_check stays non-raising.
- No silent fallback to ``orca_cli`` or ``mock``.

Tests inject HTTP and CLI stubs via ``http_client`` / ``cli_runner``.
``poll_interval_s=0.0`` disables sleep in unit tests.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Protocol

from modules.slicer.base import BaseSlicer
from modules.slicer.exceptions import (
    SlicerConfigError,
    SlicerExecutionError,
    SlicerVersionError,
)
from modules.slicer.schemas import SliceRequest, SliceResult

__all__ = ["OrcaMCPSlicer", "DEFAULT_SLICE_TOOL_NAME", "HYBRID_SLICE_TOOL_NAME"]

DEFAULT_SLICE_TOOL_NAME = "model_forge_slice"
HYBRID_SLICE_TOOL_NAME = "slice_and_stats"  # lower-level tool per commit 25b8ad3693
_DEFAULT_TIMEOUT_S = 600.0
_DEFAULT_POLL_INTERVAL_S = 1.0
_DEFAULT_INTER_SLICE_COOLDOWN_S = 0.0
_MAX_POLL_ATTEMPTS = 600


class _HTTPClient(Protocol):
    async def post(
        self, url: str, *, json: dict[str, Any], headers: dict[str, str]
    ) -> Any: ...
    async def aclose(self) -> None: ...


class _CLIRunner(Protocol):
    """Runs a subprocess command; returns (returncode, stdout, stderr)."""

    async def run(
        self, cmd: list[str], *, timeout_s: float
    ) -> tuple[int, str, str]: ...


class _DefaultCLIRunner:
    """Real asyncio subprocess runner."""

    async def run(
        self, cmd: list[str], *, timeout_s: float
    ) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise SlicerExecutionError(
                f"Orca CLI slice could not start binary {cmd[0]!r}: {exc}"
            ) from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s
            )
        except TimeoutError:
            try:
                proc.kill()
            except Exception:  # pragma: no cover
                pass
            raise SlicerExecutionError(
                f"Orca CLI slice timed out after {timeout_s:.0f}s."
            ) from None
        rc = proc.returncode if proc.returncode is not None else 1
        return rc, stdout.decode(errors="replace"), stderr.decode(errors="replace")


def _parse_print_time_s(s: str) -> float:
    """Parse OrcaSlicer print_time string to seconds.

    Handles patterns like ``'1h 30m 45s'``, ``'45m'``, ``'2h'``,
    ``'1d 2h'``.  Returns ``0.0`` if the string cannot be parsed.
    """
    total = 0.0
    for m in re.finditer(r"(\d+)\s*([dhms])", s.lower()):
        n, unit = int(m.group(1)), m.group(2)
        if unit == "d":
            total += n * 86400.0
        elif unit == "h":
            total += n * 3600.0
        elif unit == "m":
            total += n * 60.0
        elif unit == "s":
            total += float(n)
    return total


class OrcaMCPSlicer(BaseSlicer):
    """JSON-RPC adapter for the External Runtime Orca MCP binary."""

    default_adapter_name = "orca-mcp"

    def __init__(
        self,
        *,
        mcp_url: str,
        binary_path: str,
        expected_version: str,
        tool_name: str = DEFAULT_SLICE_TOOL_NAME,
        http_client: _HTTPClient | None = None,
        cli_runner: _CLIRunner | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
        inter_slice_cooldown_s: float = _DEFAULT_INTER_SLICE_COOLDOWN_S,
        adapter_label: str | None = None,
        reset_session_between_slices: bool = True,
    ) -> None:
        if not mcp_url:
            raise SlicerConfigError(
                "OrcaMCPSlicer requires a non-empty mcp_url "
                "(e.g. http://127.0.0.1:13619/mcp)."
            )
        if not binary_path:
            raise SlicerConfigError(
                "OrcaMCPSlicer requires ORCA_MCP_BINARY_PATH to be set so "
                "the adapter identity matches the running External Runtime binary "
                "(DESIGN.md R8). No mock fallback (R10)."
            )
        if not expected_version:
            raise SlicerConfigError(
                "OrcaMCPSlicer requires ORCA_MCP_EXPECTED_VERSION (e.g. "
                "0.2.0) so version drift is detected on initialize "
                "(R8). No mock fallback."
            )
        if timeout_s <= 0:
            raise SlicerConfigError("timeout_s must be > 0.")
        if inter_slice_cooldown_s < 0:
            raise SlicerConfigError("inter_slice_cooldown_s must be >= 0.")

        owns_client = http_client is None
        if http_client is None:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover - exercised via factory
                raise SlicerConfigError(
                    "httpx is not installed. Run `pip install httpx` before "
                    "selecting SLICER_ADAPTER=orca_mcp."
                ) from exc
            http_client = httpx.AsyncClient(timeout=timeout_s)

        self._mcp_url = mcp_url
        self._binary_path = str(Path(binary_path).expanduser())
        self._expected_version = expected_version
        self._tool_name = tool_name
        self._timeout_s = timeout_s
        self._poll_interval_s = poll_interval_s
        self._inter_slice_cooldown_s = inter_slice_cooldown_s
        self._client = http_client
        self._cli_runner: _CLIRunner = cli_runner or _DefaultCLIRunner()
        self._adapter_label = adapter_label or self.default_adapter_name
        self._initialized = False
        self._reported_version: str | None = None
        self._next_id = 1
        # When True (default), each slice() call closes the httpx connection
        # and resets _initialized after it completes.  This ensures that a
        # fresh MCP session is used for every job, which avoids the Orca GUI
        # thread state confusion that causes timeout on repeated calls to the
        # same process (Phase 9C-Live BLOCKER — repeatability).
        # Set to False only in unit tests that inject a pre-configured stub.
        self._reset_session_between_slices = reset_session_between_slices
        # Remember whether we were given an external client so we never close
        # an injected stub owned by the test harness.
        self._owns_client = owns_client
        # OrcaSlicer is a GUI application with shared plater/background-slice
        # state. Even when the HTTP client is async, one adapter instance must
        # drive the runtime as a single-file queue.
        self._slice_lock = asyncio.Lock()

    @property
    def adapter_name(self) -> str:
        return self._adapter_label

    @property
    def slicer_version(self) -> str:
        return self._reported_version or self._expected_version

    def health_check(self) -> bool:
        return bool(self._mcp_url and self._binary_path and self._expected_version)

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception:  # pragma: no cover - defensive
            pass

    async def _reset_session(self) -> None:
        """Close the current HTTP connection and reset MCP session state.

        Called after each successful (or failed) ``slice()`` when
        ``reset_session_between_slices=True`` so that the next job starts
        with a fresh TCP connection and a fresh MCP ``initialize`` handshake.
        This prevents Orca GUI thread state from leaking across jobs (the
        root cause of the Phase 9C-Live repeatability timeout BLOCKER).
        """
        if self._owns_client and self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001 — best effort teardown
                pass
            try:
                import httpx as _httpx

                self._client = _httpx.AsyncClient(timeout=self._timeout_s)
            except ImportError:  # pragma: no cover — guarded at __init__ time
                pass
        self._initialized = False
        self._reported_version = None
        self._next_id = 1

    async def slice(self, request: SliceRequest) -> SliceResult:
        if not request.output_dir.exists() or not request.output_dir.is_dir():
            raise SlicerConfigError(
                f"output_dir '{request.output_dir}' is missing or not a directory."
            )
        if not request.mesh_path.exists() or not request.mesh_path.is_file():
            raise SlicerConfigError(
                f"mesh_path '{request.mesh_path}' is missing or not a file."
            )

        async with self._slice_lock:
            await self._ensure_initialized()

            start = time.perf_counter()
            try:
                if self._tool_name == DEFAULT_SLICE_TOOL_NAME:
                    return await self._slice_with_model_forge_tool(request, start)
                return await self._slice_with_hybrid_flow(request, start)
            finally:
                # Reset the MCP session after every slice so the next job starts
                # with a fresh TCP connection + MCP initialize handshake. The
                # optional cooldown gives Orca's GUI task queue a short window to
                # drain delayed AddObject/backup events before the next MCP job
                # touches the plater again.
                if self._reset_session_between_slices:
                    await self._reset_session()
                    if self._inter_slice_cooldown_s > 0:
                        await asyncio.sleep(self._inter_slice_cooldown_s)

    async def _slice_with_hybrid_flow(
        self, request: SliceRequest, start: float
    ) -> SliceResult:
        """Legacy diagnostic flow (ORCA_MCP_SLICE_TOOL=slice_and_stats override).

        Steps: model_load_file → profiles → config_set → slice_and_stats →
        slice_status (poll) → model_export(3mf) → CLI --slice → G-code.
        """
        # Step 1: load mesh onto build plate
        await self._call_tool(
            "model_load_file",
            arguments={"path": str(request.mesh_path.resolve())},
        )

        # Steps 2-3: load printer + material profiles
        await self._call_tool(
            "config_load_profile",
            arguments={"name": request.printer.name},
        )
        await self._call_tool(
            "config_load_profile",
            arguments={"name": request.material.name},
        )

        # Step 4: apply print parameters
        await self._call_tool(
            "config_set",
            arguments={
                "settings": {
                    "layer_height": str(request.layer_height_mm),
                    "infill_density": f"{request.infill_percent}%",
                }
            },
        )

        # Step 5: trigger slice (returns immediately)
        await self._call_tool(self._tool_name, arguments={})

        # Step 6: poll until slicing is done, collect stats
        stats = await self._poll_slice_status()

        # Step 7: export configured project as 3MF
        threemf_path = request.output_dir / f"{request.session_id}.3mf"
        if threemf_path.exists():
            if not threemf_path.is_file():
                raise SlicerExecutionError(
                    f"Expected 3MF artifact path is not a file: {threemf_path}"
                )
            threemf_path.unlink()
        await self._call_tool(
            "model_export",
            arguments={
                "path": str(threemf_path.resolve()),
                "format": "3mf",
            },
        )
        if not threemf_path.is_file():
            raise SlicerExecutionError(
                f"model_export succeeded but 3MF not found at {threemf_path}. "
                "Check that the MCP binary has write access to the output dir."
            )

        # Step 8: CLI subprocess → G-code from the exported 3MF
        gcode_path = await self._run_cli_slice(threemf_path, request.output_dir)

        # Parse stats from slice_status
        print_time_raw = str(stats.get("print_time", ""))
        try:
            estimated_print_time_s = (
                _parse_print_time_s(print_time_raw) if print_time_raw else 0.0
            )
        except Exception:
            estimated_print_time_s = 0.0
        try:
            estimated_filament_g = float(stats.get("filament_weight_g", 0.0))
        except (TypeError, ValueError):
            estimated_filament_g = 0.0

        return SliceResult(
            gcode_path=gcode_path,
            threemf_path=threemf_path,
            adapter_used=self._adapter_label,
            slicer_version=self._reported_version or self._expected_version,
            layer_count=0,  # MCP slice_status does not expose layer count
            estimated_print_time_s=max(0.0, estimated_print_time_s),
            estimated_filament_g=max(0.0, estimated_filament_g),
            slicing_time_s=time.perf_counter() - start,
            metadata={
                "provider": "orca_mcp",
                "binary_path": self._binary_path,
                "expected_version": self._expected_version,
                "tool_name": self._tool_name,
                "print_time_raw": print_time_raw,
                "raw_stats": {
                    k: v
                    for k, v in stats.items()
                    if k
                    not in {
                        "print_time",
                        "filament_weight_g",
                        "slicing",
                        "finished",
                    }
                },
            },
        )

    async def _slice_with_model_forge_tool(
        self, request: SliceRequest, start: float
    ) -> SliceResult:
        """Use the product-level Orca MCP slicing contract."""
        data = await self._call_tool(
            self._tool_name,
            arguments={
                "mesh_path": str(request.mesh_path.resolve()),
                "output_dir": str(request.output_dir.resolve()),
                "printer_profile": request.printer.name,
                "material_profile": request.material.name,
                **(
                    {"process_profile": request.process_profile}
                    if request.process_profile
                    else {}
                ),
                "output_basename": request.session_id,
                "layer_height": request.layer_height_mm,
                "infill_density": request.infill_percent,
                "clear_plate": True,
            },
        )

        gcode_path = self._artifact_path_from_response(
            data,
            key="gcode_path",
            output_dir=request.output_dir,
            extension=".gcode",
            required=True,
        )
        threemf_path = self._artifact_path_from_response(
            data,
            key="threemf_path",
            output_dir=request.output_dir,
            extension=".3mf",
            required=False,
        )

        try:
            layer_count = int(data.get("layer_count", 0))
        except (TypeError, ValueError):
            layer_count = 0
        try:
            estimated_print_time_s = float(data.get("estimated_print_time_s", 0.0))
        except (TypeError, ValueError):
            estimated_print_time_s = 0.0
        try:
            estimated_filament_g = float(data.get("estimated_filament_g", 0.0))
        except (TypeError, ValueError):
            estimated_filament_g = 0.0

        return SliceResult(
            gcode_path=gcode_path,
            threemf_path=threemf_path,
            adapter_used=self._adapter_label,
            slicer_version=str(
                data.get("slicer_version")
                or self._reported_version
                or self._expected_version
            ),
            layer_count=max(0, layer_count),
            estimated_print_time_s=max(0.0, estimated_print_time_s),
            estimated_filament_g=max(0.0, estimated_filament_g),
            slicing_time_s=time.perf_counter() - start,
            metadata={
                "provider": "orca_mcp",
                "binary_path": self._binary_path,
                "expected_version": self._expected_version,
                "tool_name": self._tool_name,
                "selected_profiles": data.get("selected_profiles", {}),
                "process_profile": request.process_profile or "",
                "print_time_raw": data.get("print_time_raw", ""),
                "raw_stats": data.get("raw_stats", {}),
            },
        )

    def _artifact_path_from_response(
        self,
        data: dict[str, Any],
        *,
        key: str,
        output_dir: Path,
        extension: str,
        required: bool,
    ) -> Path | None:
        raw = data.get(key)
        if not isinstance(raw, str) or not raw:
            if required:
                raise SlicerExecutionError(
                    f"Orca MCP {self._tool_name} response missing {key!r}."
                )
            return None
        path = Path(raw).expanduser().resolve()
        root = output_dir.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise SlicerExecutionError(
                f"Orca MCP {self._tool_name} returned {key} outside output_dir: "
                f"{path}"
            ) from exc
        if path.suffix.lower() != extension:
            raise SlicerExecutionError(
                f"Orca MCP {self._tool_name} returned {key} with wrong "
                f"extension: {path}"
            )
        if not path.is_file():
            raise SlicerExecutionError(
                f"Orca MCP {self._tool_name} returned {key} but file does not "
                f"exist: {path}"
            )
        return path

    async def _poll_slice_status(self) -> dict[str, Any]:
        """Poll ``slice_status`` until ``finished=True``.  Returns the final stats dict."""
        for attempt in range(_MAX_POLL_ATTEMPTS):
            data = await self._call_tool("slice_status", arguments={})
            if isinstance(data.get("error"), str) and data["error"]:
                raise SlicerExecutionError(
                    f"Orca MCP slice_status reported error: {data['error']}"
                )
            if data.get("finished"):
                return data
            # After the initial few polls, if neither slicing nor finished → stuck
            if attempt > 5 and not data.get("slicing"):
                raise SlicerExecutionError(
                    "Orca MCP slice_status reports slicing=False and finished=False "
                    "after initial start. OrcaSlicer may be in an error state."
                )
            if self._poll_interval_s > 0:
                await asyncio.sleep(self._poll_interval_s)
        raise SlicerExecutionError(
            f"Orca MCP slicing did not complete within "
            f"{_MAX_POLL_ATTEMPTS * self._poll_interval_s:.0f}s."
        )

    async def _run_cli_slice(self, threemf_path: Path, output_dir: Path) -> Path:
        """Run orca-slicer CLI to produce G-code from the exported 3MF."""
        output_root = output_dir.resolve()
        before = {
            p.resolve(): p.stat().st_mtime_ns
            for p in output_root.glob("*.gcode")
            if p.is_file()
        }
        cmd = [
            self._binary_path,
            str(threemf_path.resolve()),
            "--slice",
            "0",
            "--outputdir",
            str(output_root),
        ]
        rc, stdout, stderr = await self._cli_runner.run(
            cmd, timeout_s=self._timeout_s
        )
        if rc != 0:
            raise SlicerExecutionError(
                f"Orca CLI slice exited with code {rc}. "
                f"stderr={stderr[:500]!r}"
            )
        gcode_files = []
        for candidate in sorted(output_root.glob("*.gcode")):
            if not candidate.is_file():
                continue
            resolved = candidate.resolve()
            try:
                resolved.relative_to(output_root)
            except ValueError as exc:  # pragma: no cover - glob boundary guard
                raise SlicerExecutionError(
                    f"Orca CLI emitted G-code outside output_dir: {resolved}"
                ) from exc
            previous_mtime = before.get(resolved)
            current_mtime = resolved.stat().st_mtime_ns
            if previous_mtime is None or current_mtime > previous_mtime:
                gcode_files.append(resolved)
        if not gcode_files:
            raise SlicerExecutionError(
                f"Orca CLI slice succeeded (rc=0) but no new .gcode file was "
                f"created in {output_dir}. stdout={stdout[:200]!r}"
            )
        # Return the most recently modified G-code file
        return max(gcode_files, key=lambda p: p.stat().st_mtime)

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        data = await self._jsonrpc(
            "initialize",
            params={
                "client": "model_forge",
                "expected_version": self._expected_version,
            },
        )
        server_info = data.get("serverInfo")
        if not isinstance(server_info, dict):
            server_info = data.get("server_info")
        reported = (
            server_info.get("version")
            if isinstance(server_info, dict)
            else data.get("version")
        )
        if not isinstance(reported, str) or not reported:
            raise SlicerExecutionError(
                "Orca MCP initialize response missing server version."
            )
        if reported != self._expected_version:
            raise SlicerVersionError(
                f"Orca MCP reports version {reported!r} but adapter is "
                f"pinned to {self._expected_version!r} (R8). Rebuild the "
                "External Runtime MCP binary or update ORCA_MCP_EXPECTED_VERSION."
            )
        self._reported_version = reported
        self._initialized = True

    async def _call_tool(
        self, name: str, *, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        result = await self._jsonrpc(
            "tools/call", params={"name": name, "arguments": arguments}
        )
        if "content" not in result and "isError" not in result:
            # Unit-test/backward-compat shape used by earlier stubs.
            return result

        content = result.get("content")
        text = ""
        if isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict) and isinstance(first.get("text"), str):
                text = first["text"]
        if result.get("isError"):
            raise SlicerExecutionError(
                f"Orca MCP tool {name!r} failed: {text or result!r}"
            )
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SlicerExecutionError(
                f"Orca MCP tool {name!r} returned non-JSON text: {text[:200]!r}"
            ) from exc
        if not isinstance(parsed, dict):
            raise SlicerExecutionError(
                f"Orca MCP tool {name!r} returned JSON that is not an object."
            )
        return parsed

    async def _jsonrpc(
        self, method: str, *, params: dict[str, Any]
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        try:
            response = await self._client.post(
                self._mcp_url,
                json=body,
                headers={"Content-Type": "application/json"},
            )
        except Exception as exc:
            raise SlicerExecutionError(
                f"Orca MCP transport failure for method {method!r}: {exc}"
            ) from exc

        status = getattr(response, "status_code", None)
        if isinstance(status, int) and status >= 400:
            text = getattr(response, "text", "")
            raise SlicerExecutionError(
                f"Orca MCP {method} HTTP {status}: {text[:200]}"
            )

        try:
            payload = response.json()
        except Exception as exc:
            raise SlicerExecutionError(
                f"Orca MCP {method} response was not valid JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise SlicerExecutionError(
                f"Orca MCP {method} response was not a JSON object."
            )

        if payload.get("jsonrpc") != "2.0":
            raise SlicerExecutionError(
                f"Orca MCP {method} response had invalid jsonrpc marker: "
                f"{payload.get('jsonrpc')!r}."
            )
        if payload.get("id") != request_id:
            raise SlicerExecutionError(
                f"Orca MCP {method} response id mismatch: expected "
                f"{request_id!r}, got {payload.get('id')!r}."
            )

        if "error" in payload and payload["error"] is not None:
            err = payload["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            code = err.get("code") if isinstance(err, dict) else None
            raise SlicerExecutionError(
                f"Orca MCP {method} returned JSON-RPC error "
                f"(code={code}): {msg}"
            )

        result = payload.get("result")
        if not isinstance(result, dict):
            raise SlicerExecutionError(
                f"Orca MCP {method} response missing object 'result'."
            )
        return result
