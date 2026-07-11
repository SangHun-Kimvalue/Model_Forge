"""CadQuery / Build123d mechanical CAD adapter (Phase 9A).

Wraps LLM-authored CadQuery or Build123d scripts in an export footer and
executes them through an injected ``SandboxRunner``.  The adapter itself
performs no geometry operations — all execution happens inside the runner's
isolation boundary (Docker or, for local dev only, a subprocess).

The exported artifact is always named ``sandbox-result.<format>`` in
``request.output_dir``, matching the runner contract in ``runner.py``.

Dialect contract:
    Generated scripts must define a ``main()`` function that returns the
    final workplane / assembly (cadquery) or part (build123d).  The adapter
    appends a footer that calls ``main()`` and exports the result.

Supported dialects: CADQUERY, BUILD123D.
Supported output formats: stl.
"""

from __future__ import annotations

import time

from modules.cad_mechanical.base import BaseMechanicalCADGenerator
from modules.cad_mechanical.exceptions import GenerationError
from modules.cad_mechanical.runner import (
    SANDBOX_ARTIFACT_STEM,
    RejectAllRunner,
    SandboxRunner,
    SubprocessSandboxRunner,
)
from modules.cad_mechanical.schemas import CADDialect, GenerationRequest, GenerationResult

__all__ = ["CadQueryAdapter"]


class CadQueryAdapter(BaseMechanicalCADGenerator):
    """Mechanical CAD adapter that executes CadQuery / Build123d via a SandboxRunner.

    The adapter appends a deterministic export footer to the LLM-authored
    script so the runner's contract (``sandbox-result.<fmt>`` in the CWD)
    is satisfied without the LLM needing to know about the artifact name.

    Preconditions:
        ``runner`` must already be configured with a matching ``sandbox_root``.
        The runner's ``health_check()`` is called by ``health_check()`` below.
        Docker mode checks the runner/container boundary; local subprocess
        mode also requires host cadquery/build123d imports.
    Raises:
        ``SandboxError`` subclasses — sandbox-level failures (allowlist
            violation, timeout, OOM, Docker unavailable).
        ``GenerationError`` — missing artifacts, unsupported dialect/format,
            or unexpected errors wrapping the runner response.
    """

    default_adapter_name = "cadquery-v1"
    _SUPPORTED_DIALECTS = frozenset({CADDialect.CADQUERY, CADDialect.BUILD123D})
    _SUPPORTED_FORMATS = frozenset({"stl"})

    def __init__(
        self,
        *,
        runner: SandboxRunner | None = None,
        adapter_label: str | None = None,
    ) -> None:
        self._runner = runner if runner is not None else RejectAllRunner()
        self._adapter_label = adapter_label or self.default_adapter_name

    @property
    def adapter_name(self) -> str:
        return self._adapter_label

    def health_check(self) -> bool:
        """Return True only when the selected runner boundary is ready."""
        if isinstance(self._runner, SubprocessSandboxRunner) and not (
            _cadquery_importable() and _build123d_importable()
        ):
            return False
        return self._runner.health_check()

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        if request.dialect not in self._SUPPORTED_DIALECTS:
            raise GenerationError(
                f"CadQueryAdapter supports "
                f"{sorted(d.value for d in self._SUPPORTED_DIALECTS)} dialects; "
                f"got '{request.dialect.value}'."
            )
        if request.format not in self._SUPPORTED_FORMATS:
            raise GenerationError(
                f"CadQueryAdapter supports {sorted(self._SUPPORTED_FORMATS)} "
                f"output formats; got '{request.format}'."
            )

        wrapped_code = _wrap_with_export(request.code, request.dialect, request.format)
        wrapped_request = request.model_copy(update={"code": wrapped_code})

        start = time.perf_counter()
        sandbox_result = await self._runner.execute(wrapped_request)
        elapsed = time.perf_counter() - start

        return GenerationResult(
            path=sandbox_result.artifact_path,
            format=request.format,
            dialect=request.dialect,
            adapter_used=self._adapter_label,
            generation_time_s=elapsed,
            metadata={
                "sandboxed": True,
                "runner": self._runner.runner_name,
                "stdout_head": sandbox_result.stdout[:500],
                "stderr_head": sandbox_result.stderr[:500],
            },
        )


def _cadquery_importable() -> bool:
    try:
        import cadquery  # noqa: F401

        return True
    except ImportError:
        return False


def _build123d_importable() -> bool:
    try:
        import build123d  # noqa: F401

        return True
    except ImportError:
        return False


def _wrap_with_export(code: str, dialect: CADDialect, fmt: str) -> str:
    """Append an export footer to the LLM-authored script.

    The footer calls ``main()`` (the expected entrypoint defined by the CAD
    coder agent) and exports the result to ``sandbox-result.<fmt>`` in the
    current working directory, which the runner sets to ``output_dir``.

    Both footers only use imports from the sandbox allowlist
    (cadquery / build123d) and do not call any forbidden builtins.
    """
    artifact_name = f"{SANDBOX_ARTIFACT_STEM}.{fmt}"
    if dialect == CADDialect.CADQUERY:
        return (
            code
            + "\n# ---- Model Forge CadQueryAdapter export footer ----\n"
            "import cadquery as _cq_export\n"
            "_model_forge_result = main()\n"
            f"_cq_export.exporters.export(_model_forge_result, {artifact_name!r})\n"
        )
    if dialect == CADDialect.BUILD123D:
        return (
            code
            + "\n# ---- Model Forge CadQueryAdapter export footer ----\n"
            "import build123d as _b123d_export\n"
            "_model_forge_result = main()\n"
            f"_b123d_export.export_stl(_model_forge_result, {artifact_name!r})\n"
        )
    raise GenerationError(
        f"No export footer defined for dialect '{dialect.value}'. "
        "Only cadquery and build123d are supported by CadQueryAdapter."
    )
