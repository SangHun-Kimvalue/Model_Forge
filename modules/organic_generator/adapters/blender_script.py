"""Gemini/LLM-backed Blender headless organic generator.

This adapter is a POC bridge: an LLM writes a constrained Blender Python
``build_scene()`` function, then Blender runs headless and exports OBJ/STL.
It is intentionally local-subprocess only; product/untrusted execution needs a
separate Docker boundary before this path is exposed outside a single-user POC.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Protocol

from modules.llm.base import BaseLLMProvider
from modules.llm.exceptions import LLMProviderError
from modules.llm.schemas import LLMMessage, LLMRequest
from modules.organic_generator.base import BaseOrganicGenerator
from modules.organic_generator.exceptions import (
    GenerationError,
    GenerationTimeout,
    OrganicGeneratorConfigError,
)
from modules.organic_generator.schemas import GenerationRequest, GenerationResult

__all__ = ["BlenderScriptOrganicGenerator"]

_SCRIPT_NAME = "_model_forge_blender_scene.py"
_CODE_FENCE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)
_ENTRYPOINT_RE = re.compile(r"\bdef\s+build_scene\s*\(")
_FORBIDDEN_RE = re.compile(
    r"(?im)\b(?:open|exec|eval|__import__)\s*\(|\b(?:os|sys|subprocess|socket|requests|pathlib)\b"
)
_SUPPORTED_FORMATS = frozenset({"obj", "stl"})


class _CommandRunner(Protocol):
    def health_check(
        self, *, binary_path: str, expected_version: str | None
    ) -> bool: ...

    async def run(
        self, command: list[str], *, cwd: Path, timeout_s: float
    ) -> tuple[int, str, str]: ...


class _DefaultBlenderRunner:
    def health_check(
        self, *, binary_path: str, expected_version: str | None
    ) -> bool:
        resolved = _resolve_binary(binary_path)
        if resolved is None:
            return False
        if not expected_version:
            return True
        try:
            completed = subprocess.run(
                [resolved, "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        version_text = f"{completed.stdout}\n{completed.stderr}"
        return completed.returncode == 0 and expected_version in version_text

    async def run(
        self, command: list[str], *, cwd: Path, timeout_s: float
    ) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
            )
        except OSError as exc:
            raise GenerationError(
                f"Blender could not start binary {command[0]!r}: {exc}"
            ) from exc

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s
            )
        except TimeoutError as exc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            raise GenerationTimeout(
                f"Blender headless generation exceeded {timeout_s:.1f}s timeout."
            ) from exc

        return (
            proc.returncode if proc.returncode is not None else 1,
            stdout_b.decode("utf-8", errors="replace"),
            stderr_b.decode("utf-8", errors="replace"),
        )


class BlenderScriptOrganicGenerator(BaseOrganicGenerator):
    """Generate procedural organic meshes with an LLM-authored Blender script."""

    default_adapter_name = "blender-script-organic-v1"

    def __init__(
        self,
        *,
        provider: BaseLLMProvider,
        blender_binary_path: str = "blender",
        expected_version: str | None = None,
        timeout_s: float = 90.0,
        adapter_label: str | None = None,
        runner: _CommandRunner | None = None,
    ) -> None:
        if not blender_binary_path:
            raise OrganicGeneratorConfigError("BLENDER_BINARY_PATH must not be empty.")
        if timeout_s <= 0:
            raise OrganicGeneratorConfigError("ORGANIC_BLENDER_TIMEOUT_S must be > 0.")
        self._provider = provider
        self._blender_binary_path = blender_binary_path
        self._expected_version = expected_version
        self._timeout_s = timeout_s
        self._adapter_label = adapter_label or self.default_adapter_name
        self._runner = runner or _DefaultBlenderRunner()

    @property
    def provider_name(self) -> str:
        return "blender_script"

    def health_check(self) -> bool:
        return self._runner.health_check(
            binary_path=self._blender_binary_path,
            expected_version=self._expected_version,
        )

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        if request.format not in _SUPPORTED_FORMATS:
            raise GenerationError(
                f"BlenderScriptOrganicGenerator supports "
                f"{sorted(_SUPPORTED_FORMATS)}; got {request.format!r}."
            )
        output_dir = request.output_dir.resolve()
        if not output_dir.exists() or not output_dir.is_dir():
            raise GenerationError(f"output_dir '{request.output_dir}' must exist.")

        binary = _resolve_binary(self._blender_binary_path)
        if binary is None:
            raise OrganicGeneratorConfigError(
                f"Blender binary {self._blender_binary_path!r} was not found. "
                "Set BLENDER_BINARY_PATH to blender.exe."
            )

        code = await self._generate_blender_code(request)
        _validate_blender_code(code)

        script_path = output_dir / _SCRIPT_NAME
        artifact_path = output_dir / f"organic-blender-result.{request.format}"
        _remove_stale_file(script_path, label="Blender script")
        _remove_stale_file(artifact_path, label="Blender artifact")
        script_path.write_text(
            _wrap_script(code, artifact_path=artifact_path, fmt=request.format),
            encoding="utf-8",
        )

        command = [
            binary,
            "--background",
            "--factory-startup",
            "--python",
            script_path.name,
        ]
        start = time.monotonic()
        rc, stdout, stderr = await self._runner.run(
            command, cwd=output_dir, timeout_s=self._timeout_s
        )
        elapsed = time.monotonic() - start
        if rc != 0:
            raise GenerationError(
                f"Blender headless generation failed with exit code {rc}: "
                f"{stderr.strip() or stdout.strip()}"
            )
        _validate_artifact(artifact_path=artifact_path, output_dir=output_dir)
        return GenerationResult(
            path=artifact_path,
            format=request.format,
            adapter_used=self._adapter_label,
            generation_time_s=elapsed,
            metadata={
                "provider": self.provider_name,
                "llm_provider": self._provider.provider_name,
                "boundary": "blender-headless-subprocess",
                "sandboxed": False,
                "expected_version": self._expected_version,
                "stdout_head": stdout[:500],
                "stderr_head": stderr[:500],
            },
        )

    async def _generate_blender_code(self, request: GenerationRequest) -> str:
        try:
            response = await self._provider.complete(
                LLMRequest(
                    messages=(
                        LLMMessage(role="system", content=_system_prompt()),
                        LLMMessage(role="user", content=_user_prompt(request)),
                    ),
                    temperature=0.2,
                    max_tokens=4096,
                    trace_id=f"{request.session_id}:organic",
                )
            )
        except LLMProviderError as exc:
            raise GenerationError(
                f"LLM provider '{self._provider.provider_name}' failed while "
                f"generating Blender script: {exc}"
            ) from exc
        return _extract_code(response.content)


def _system_prompt() -> str:
    return (
        "You are a senior Blender procedural modeling engineer. Write a single "
        "self-contained Python function named build_scene(). It may use bpy, "
        "math, and mathutils only. The function should create a small organic "
        "or decorative mesh centered near the origin, with printable thickness "
        "where possible. Do not write files, do not read files, do not use "
        "network/process APIs, and do not call build_scene(). Respond with only "
        "one fenced Python code block."
    )


def _user_prompt(request: GenerationRequest) -> str:
    negative = request.negative_prompt or "none"
    return (
        f"Prompt: {request.prompt.strip()}\n"
        f"Quality: {request.quality.value}\n"
        f"Target format: {request.format}\n"
        f"Seed: {request.seed if request.seed is not None else 'none'}\n"
        f"Negative prompt: {negative}\n"
        "Keep the mesh compact enough for a desktop FDM preview."
    )


def _extract_code(content: str) -> str:
    match = _CODE_FENCE_RE.search(content)
    if match:
        return match.group(1).strip() + "\n"
    stripped = content.strip()
    if _ENTRYPOINT_RE.search(stripped):
        return stripped + "\n"
    return ""


def _validate_blender_code(code: str) -> None:
    if not code.strip():
        raise GenerationError("LLM returned an empty Blender script.")
    if not _ENTRYPOINT_RE.search(code):
        raise GenerationError("Blender script must define build_scene().")
    if _FORBIDDEN_RE.search(code):
        raise GenerationError(
            "Blender script contains forbidden filesystem/process/network tokens."
        )


def _wrap_script(code: str, *, artifact_path: Path, fmt: str) -> str:
    output = str(artifact_path).replace("\\", "\\\\")
    exporter = (
        f'bpy.ops.wm.obj_export(filepath=r"{output}")'
        if fmt == "obj"
        else f'bpy.ops.wm.stl_export(filepath=r"{output}")'
    )
    return f'''
import bpy
import math
from mathutils import Vector

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete()

{code}

build_scene()

mesh_objects = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
if not mesh_objects:
    raise RuntimeError("build_scene() did not create any mesh objects")

for obj in mesh_objects:
    obj.select_set(True)
bpy.context.view_layer.objects.active = mesh_objects[0]

{exporter}
'''


def _resolve_binary(binary_path: str) -> str | None:
    path = Path(binary_path).expanduser()
    if path.parent != Path(".") or path.is_absolute():
        return str(path.resolve()) if path.is_file() else None
    return shutil.which(binary_path)


def _remove_stale_file(path: Path, *, label: str) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        raise GenerationError(f"{label} path is a directory, not a file: {path}")
    try:
        path.unlink()
    except OSError as exc:
        raise GenerationError(f"Failed to remove stale {label}: {path}") from exc


def _validate_artifact(*, artifact_path: Path, output_dir: Path) -> None:
    resolved_artifact = artifact_path.resolve()
    if not resolved_artifact.is_relative_to(output_dir.resolve()):
        raise GenerationError(f"Blender artifact escaped output_dir: {artifact_path}.")
    if not resolved_artifact.exists() or not resolved_artifact.is_file():
        raise GenerationError(
            f"Blender completed but did not create '{artifact_path.name}'."
        )
    if resolved_artifact.stat().st_size <= 0:
        raise GenerationError(f"Blender produced empty artifact '{artifact_path.name}'.")
