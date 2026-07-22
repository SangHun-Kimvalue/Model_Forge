"""Fail-closed adapters for subscription-backed capable-model CLIs."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from modules.llm.base import BaseLLMProvider
from modules.llm.exceptions import (
    LLMProviderConfigError,
    LLMProviderError,
    LLMTimeoutError,
)
from modules.llm.schemas import LLMRequest, LLMResponse, LLMUsage

__all__ = ["CliLLMProvider"]

_SUPPORTED_BACKENDS = frozenset({"codex", "claude"})
_STDOUT_LIMIT_BYTES = 4 * 1024 * 1024
_STDERR_LIMIT_BYTES = 64 * 1024
_STDERR_EXCERPT_CHARS = 2_000
_READ_CHUNK_BYTES = 64 * 1024
_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|authorization|token|password|secret)(\s*[:=]\s*)(\S+)"
)


@dataclass(frozen=True, slots=True)
class CliResult:
    """Bounded and strictly decoded process result."""

    returncode: int
    stdout: str
    stderr: str


class _CliRunner(Protocol):
    async def run(
        self,
        argv: Sequence[str],
        *,
        stdin_text: str,
        timeout_s: float,
    ) -> CliResult:
        """Run one CLI request and complete teardown before returning or raising."""


class _Process(Protocol):
    pid: int
    returncode: int | None
    stdin: asyncio.StreamWriter | None
    stdout: asyncio.StreamReader | None
    stderr: asyncio.StreamReader | None

    async def wait(self) -> int: ...

    def kill(self) -> None: ...


_ProcessFactory = Callable[..., Awaitable[_Process]]


class _OutputLimitExceeded(LLMProviderError):
    pass


class _SubprocessCliRunner:
    """Async subprocess runner with bounded capture and process-tree teardown."""

    def __init__(
        self,
        *,
        process_factory: _ProcessFactory | None = None,
        stdout_limit_bytes: int = _STDOUT_LIMIT_BYTES,
        stderr_limit_bytes: int = _STDERR_LIMIT_BYTES,
    ) -> None:
        if stdout_limit_bytes <= 0 or stderr_limit_bytes <= 0:
            raise ValueError("CLI output byte limits must be positive.")
        if process_factory is None:
            self._process_factory = cast(_ProcessFactory, asyncio.create_subprocess_exec)
        else:
            self._process_factory = process_factory
        self._stdout_limit_bytes = stdout_limit_bytes
        self._stderr_limit_bytes = stderr_limit_bytes

    async def run(
        self,
        argv: Sequence[str],
        *,
        stdin_text: str,
        timeout_s: float,
    ) -> CliResult:
        if not argv:
            raise LLMProviderConfigError("CLI argv must not be empty.")
        if timeout_s <= 0:
            raise LLMProviderConfigError("CLI timeout must be positive.")

        kwargs: dict[str, Any] = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000200  # CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True

        process = await self._process_factory(*argv, **kwargs)
        try:
            stdout_raw, stderr_raw, returncode = await asyncio.wait_for(
                self._exchange(process, stdin_text),
                timeout=timeout_s,
            )
        except TimeoutError as exc:
            await self._terminate_tree(process)
            raise LLMTimeoutError("CLI request timed out; partial output was discarded.") from exc
        except asyncio.CancelledError:
            await self._terminate_tree(process)
            raise
        except BaseException:
            await self._terminate_tree(process)
            raise

        try:
            stdout = stdout_raw.decode("utf-8")
            stderr = stderr_raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LLMProviderError("CLI output is not valid UTF-8.") from exc
        return CliResult(returncode=returncode, stdout=stdout, stderr=stderr)

    async def _exchange(self, process: _Process, stdin_text: str) -> tuple[bytes, bytes, int]:
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise LLMProviderError("CLI process pipes were not created.")
        process.stdin.write(stdin_text.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()
        await process.stdin.wait_closed()
        stdout_task = asyncio.create_task(
            self._read_bounded(process.stdout, self._stdout_limit_bytes, "stdout")
        )
        stderr_task = asyncio.create_task(
            self._read_bounded(process.stderr, self._stderr_limit_bytes, "stderr")
        )
        stdout, stderr, returncode = await asyncio.gather(
            stdout_task,
            stderr_task,
            process.wait(),
        )
        return stdout, stderr, returncode

    @staticmethod
    async def _read_bounded(
        stream: asyncio.StreamReader,
        limit_bytes: int,
        stream_name: str,
    ) -> bytes:
        collected = bytearray()
        while True:
            chunk = await stream.read(_READ_CHUNK_BYTES)
            if not chunk:
                return bytes(collected)
            collected.extend(chunk)
            if len(collected) > limit_bytes:
                raise _OutputLimitExceeded(
                    f"CLI {stream_name} exceeded the {limit_bytes}-byte safety limit."
                )

    @staticmethod
    async def _terminate_tree(process: _Process) -> None:
        if process.returncode is not None:
            await process.wait()
            return
        if os.name == "nt":
            try:
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
            except OSError:
                process.kill()
        else:
            try:
                killpg = cast(Callable[[int, int], None], os.__dict__["killpg"])
                sigkill = cast(int, signal.__dict__["SIGKILL"])
                killpg(process.pid, sigkill)
            except ProcessLookupError:
                pass
            except OSError:
                process.kill()
        await process.wait()


class CliLLMProvider(BaseLLMProvider):
    """Raw-text provider over a pinned, inference-only CLI invocation.

    Preconditions:
        ``backend`` is codex or claude, ``cli_bin`` resolves to a supported shim,
        and ``timeout_s`` is positive.
    Postconditions:
        Returns unmodified non-blank stdout and best-effort zero usage.
    Raises:
        LLMProviderConfigError for unsupported controls or executable settings;
        LLMTimeoutError for timeout; LLMProviderError for all other CLI failures.
    """

    def __init__(
        self,
        *,
        backend: str,
        cli_bin: str,
        model: str | None = None,
        timeout_s: float = 120.0,
        runner: _CliRunner | None = None,
    ) -> None:
        normalized_backend = backend.strip().lower()
        if normalized_backend not in _SUPPORTED_BACKENDS:
            raise LLMProviderConfigError(
                "CLI_BACKEND must be one of: codex, claude. Mock fallback is forbidden."
            )
        if timeout_s <= 0:
            raise LLMProviderConfigError("CLI_TIMEOUT_S must be positive.")
        self._backend = normalized_backend
        self._command_prefix = _resolve_cli_command(cli_bin)
        self._model = _optional_nonblank(model, "CLI_MODEL")
        self._timeout_s = timeout_s
        self._runner = runner or _SubprocessCliRunner()

    @property
    def provider_name(self) -> str:
        return f"cli:{self._backend}"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if request.provider_options:
            raise LLMProviderConfigError(
                "CLI provider_options are unsupported and must be empty."
            )
        selected_model = _optional_nonblank(request.model, "LLMRequest.model") or self._model
        argv = _build_cli_argv(
            self._backend,
            self._command_prefix,
            model=selected_model,
        )
        prompt = _build_prompt(request)
        try:
            result = await self._runner.run(
                argv,
                stdin_text=prompt,
                timeout_s=self._timeout_s,
            )
            if result.returncode != 0:
                detail = _safe_stderr_excerpt(result.stderr)
                suffix = f" Stderr: {detail}" if detail else ""
                raise LLMProviderError(
                    f"CLI backend '{self._backend}' exited with code "
                    f"{result.returncode}.{suffix}"
                )
            if not result.stdout.strip():
                raise LLMProviderError(
                    "CLI returned an empty response; refusing to fabricate content."
                )
            return LLMResponse(
                provider=self.provider_name,
                model=selected_model or "backend-default",
                content=result.stdout,
                usage=LLMUsage(input_tokens=0, output_tokens=0),
                trace_id=request.trace_id,
                raw_metadata={
                    "backend": self._backend,
                    "temperature_applied": False,
                },
            )
        except LLMProviderError:
            raise
        except FileNotFoundError as exc:
            raise LLMProviderConfigError(
                f"CLI executable was not found for backend '{self._backend}'."
            ) from exc
        except OSError as exc:
            raise LLMProviderError(f"CLI process failed to start: {exc!s}") from exc
        except Exception as exc:
            raise LLMProviderError(f"CLI request failed: {exc!s}") from exc


def _resolve_cli_command(cli_bin: str) -> tuple[str, ...]:
    stripped = cli_bin.strip()
    if not stripped:
        raise LLMProviderConfigError("CLI_BIN must not be blank.")
    candidate = Path(stripped).expanduser()
    if candidate.parent != Path(".") or candidate.is_absolute():
        resolved = str(candidate.resolve()) if candidate.exists() else None
    else:
        resolved = shutil.which(stripped)
    if resolved is None:
        raise LLMProviderConfigError(
            f"CLI_BIN '{stripped}' could not be resolved. Mock fallback is forbidden."
        )

    suffix = Path(resolved).suffix.lower()
    if os.name == "nt":
        if suffix == ".ps1":
            powershell = shutil.which("powershell")
            if powershell is None:
                raise LLMProviderConfigError("PowerShell is required to execute a .ps1 CLI shim.")
            return powershell, "-NoProfile", "-NonInteractive", "-File", resolved
        if suffix in {".cmd", ".bat"}:
            command_shell = os.environ.get("COMSPEC") or shutil.which("cmd.exe")
            if not command_shell:
                raise LLMProviderConfigError("cmd.exe is required to execute a CLI shim.")
            return command_shell, "/D", "/S", "/C", "call", resolved
        if suffix not in {"", ".exe", ".com"}:
            raise LLMProviderConfigError(f"Unsupported Windows CLI shim type: {suffix}")
    elif suffix == ".ps1":
        raise LLMProviderConfigError("PowerShell CLI shims are supported only on Windows.")
    return (resolved,)


def _build_cli_argv(
    backend: str,
    command_prefix: Sequence[str],
    *,
    model: str | None,
) -> tuple[str, ...]:
    if backend == "codex":
        argv = [
            *command_prefix,
            "exec",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--config",
            "features.shell_tool=false",
            "--config",
            "features.unified_exec=false",
            "--config",
            'web_search="disabled"',
            "--config",
            "apps._default.enabled=false",
            "--config",
            "mcp_servers={}",
            "--config",
            "hooks={}",
        ]
    elif backend == "claude":
        argv = [
            *command_prefix,
            "--print",
            "--safe-mode",
            "--disable-slash-commands",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--tools",
            "",
            "--settings",
            "{}",
            "--setting-sources",
            "",
            "--no-session-persistence",
            "--permission-mode",
            "dontAsk",
            "--output-format",
            "text",
        ]
    else:
        raise LLMProviderConfigError(f"Unsupported CLI backend: {backend}")
    if model is not None:
        argv.extend(("--model", model))
    if backend == "codex":
        argv.append("-")
    return tuple(argv)


def _build_prompt(request: LLMRequest) -> str:
    return "\n\n".join(f"[{message.role}]\n{message.content}" for message in request.messages)


def _optional_nonblank(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        raise LLMProviderConfigError(f"{label} must not be blank when specified.")
    return stripped


def _safe_stderr_excerpt(stderr: str) -> str:
    redacted = _SECRET_RE.sub(r"\1\2[REDACTED]", stderr)
    if len(redacted) > _STDERR_EXCERPT_CHARS:
        return redacted[:_STDERR_EXCERPT_CHARS] + "...[truncated]"
    return redacted
