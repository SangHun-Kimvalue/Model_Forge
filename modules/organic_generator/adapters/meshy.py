"""Meshy text-to-3D organic generator adapter (Phase 8B scaffold).

Drives the Meshy v2 text-to-3D HTTP API.

Fail-fast semantics:
- ``api_key`` is required at construction time. Missing key →
  ``OrganicGeneratorConfigError``. No mock fallback (DESIGN.md R10).
- ``httpx`` is imported lazily so installing it is only required when
  ``ORGANIC_GENERATOR_ADAPTER=meshy``.
- Only ``obj`` output is supported in this scaffold; Meshy returns
  ``glb`` by default and OBJ is the closest match to our pipeline's
  ``MeshFormat`` contract. ``stl`` requests raise ``GenerationError``
  with a clear remediation message.
- Network/protocol failures raise ``GenerationError`` / ``GenerationTimeout``.

Tests inject an HTTP client stub via ``http_client``. The stub must
expose ``post(url, *, json, headers)``, ``get(url, *, headers)``, and
``aclose()``. Real callers get a configured ``httpx.AsyncClient``.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, Protocol
from urllib.parse import urlparse

from modules.organic_generator.base import BaseOrganicGenerator
from modules.organic_generator.exceptions import (
    GenerationError,
    GenerationTimeout,
    OrganicGeneratorConfigError,
)
from modules.organic_generator.schemas import GenerationRequest, GenerationResult

__all__ = ["MeshyOrganicGenerator"]

_DEFAULT_BASE_URL = "https://api.meshy.ai"
_CREATE_PATH = "/openapi/v2/text-to-3d"


class _HTTPClient(Protocol):
    async def post(
        self, url: str, *, json: dict[str, Any], headers: dict[str, str]
    ) -> Any: ...
    async def get(self, url: str, *, headers: dict[str, str]) -> Any: ...
    async def aclose(self) -> None: ...


class MeshyOrganicGenerator(BaseOrganicGenerator):
    """Real text-to-3D adapter for the Meshy v2 API."""

    default_adapter_name = "meshy-text-to-3d"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = _DEFAULT_BASE_URL,
        http_client: _HTTPClient | None = None,
        poll_interval_s: float = 2.0,
        timeout_s: float = 600.0,
        adapter_label: str | None = None,
        art_style: str = "realistic",
    ) -> None:
        if not api_key:
            raise OrganicGeneratorConfigError(
                "MeshyOrganicGenerator requires a non-empty api_key. "
                "Set MESHY_API_KEY. Mock fallback is forbidden "
                "(DESIGN.md R10)."
            )
        if poll_interval_s <= 0:
            raise OrganicGeneratorConfigError(
                "poll_interval_s must be > 0."
            )
        if timeout_s <= 0:
            raise OrganicGeneratorConfigError("timeout_s must be > 0.")

        if http_client is None:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover - exercised via factory
                raise OrganicGeneratorConfigError(
                    "httpx is not installed. Run `pip install httpx` or "
                    "pin it in your deployment requirements before "
                    "selecting ORGANIC_GENERATOR_ADAPTER=meshy."
                ) from exc
            http_client = httpx.AsyncClient(timeout=timeout_s)

        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = http_client
        self._poll_interval_s = poll_interval_s
        self._timeout_s = timeout_s
        self._adapter_label = adapter_label or self.default_adapter_name
        self._art_style = art_style

    @property
    def provider_name(self) -> str:
        return "meshy"

    def health_check(self) -> bool:
        return bool(self._api_key)

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception:  # pragma: no cover - defensive
            pass

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        if not request.output_dir.exists() or not request.output_dir.is_dir():
            raise GenerationError(
                f"output_dir '{request.output_dir}' must exist and be a directory."
            )
        if request.format != "obj":
            raise GenerationError(
                "MeshyOrganicGenerator only emits 'obj' in this build; "
                f"got '{request.format}'. Request 'obj' or implement a "
                "GLB→STL conversion step before changing this contract."
            )

        start = time.perf_counter()
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        task_id = await self._create_preview_task(request, headers)
        download_url = await self._poll_until_ready(task_id, headers)
        payload = await self._download(download_url)

        digest = hashlib.sha256(payload).hexdigest()[:12]
        path = request.output_dir / f"meshy-{digest}.{request.format}"
        try:
            path.write_bytes(payload)
        except OSError as exc:
            raise GenerationError(
                f"failed to write Meshy mesh to '{path}': {exc}"
            ) from exc

        return GenerationResult(
            path=path,
            format=request.format,
            adapter_used=self._adapter_label,
            generation_time_s=time.perf_counter() - start,
            metadata={
                "provider": self.provider_name,
                "task_id": task_id,
                "digest": digest,
                "quality": request.quality.value,
            },
        )

    async def _create_preview_task(
        self, request: GenerationRequest, headers: dict[str, str]
    ) -> str:
        body: dict[str, Any] = {
            "mode": "preview",
            "prompt": request.prompt,
            "art_style": self._art_style,
        }
        if request.negative_prompt:
            body["negative_prompt"] = request.negative_prompt
        if request.seed is not None:
            body["seed"] = request.seed

        try:
            response = await self._client.post(
                f"{self._base_url}{_CREATE_PATH}", json=body, headers=headers
            )
        except Exception as exc:
            raise GenerationError(
                f"Meshy create preview task transport failure: {exc}"
            ) from exc
        data = _read_json(response, "create preview task")
        task_id = data.get("result") or data.get("id") or data.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise GenerationError(
                "Meshy create-task response is missing a string task id."
            )
        return task_id

    async def _poll_until_ready(
        self, task_id: str, headers: dict[str, str]
    ) -> str:
        deadline = time.monotonic() + self._timeout_s
        url = f"{self._base_url}{_CREATE_PATH}/{task_id}"

        while True:
            try:
                response = await self._client.get(url, headers=headers)
            except Exception as exc:
                raise GenerationError(
                    f"Meshy poll task transport failure: {exc}"
                ) from exc
            data = _read_json(response, "poll task")
            status = str(data.get("status", "")).upper()

            if status in {"SUCCEEDED", "SUCCESS"}:
                download_url = _extract_obj_url(data)
                if download_url is None:
                    raise GenerationError(
                        "Meshy task succeeded but no obj url was returned."
                    )
                return download_url
            if status in {"FAILED", "CANCELED", "EXPIRED"}:
                raise GenerationError(
                    f"Meshy task '{task_id}' ended with status={status}."
                )

            if time.monotonic() >= deadline:
                raise GenerationTimeout(
                    f"Meshy task '{task_id}' did not finish within "
                    f"{self._timeout_s:.0f}s."
                )
            await asyncio.sleep(self._poll_interval_s)

    async def _download(self, url: str) -> bytes:
        try:
            response = await self._client.get(
                url, headers=_download_headers(url, self._base_url)
            )
        except Exception as exc:
            raise GenerationError(
                f"Meshy download mesh transport failure: {exc}"
            ) from exc
        _raise_for_status(response, "download mesh")
        payload = getattr(response, "content", None)
        if not isinstance(payload, bytes) or not payload:
            raise GenerationError(
                "Meshy mesh download returned an empty body."
            )
        return payload


def _read_json(response: Any, context: str) -> dict[str, Any]:
    _raise_for_status(response, context)
    try:
        data = response.json()
    except Exception as exc:
        raise GenerationError(
            f"Meshy {context} response was not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise GenerationError(
            f"Meshy {context} response was not a JSON object."
        )
    return data


def _raise_for_status(response: Any, context: str) -> None:
    status = getattr(response, "status_code", None)
    if isinstance(status, int) and status >= 400:
        text = getattr(response, "text", "")
        raise GenerationError(
            f"Meshy {context} HTTP {status}: {text[:200]}"
        )


def _extract_obj_url(data: dict[str, Any]) -> str | None:
    urls = data.get("model_urls")
    if isinstance(urls, dict):
        candidate = urls.get("obj")
        if isinstance(candidate, str) and candidate:
            return candidate
    # Alternate field names some versions use:
    candidate = data.get("model_url")
    if isinstance(candidate, str) and candidate:
        return candidate
    return None


def _download_headers(url: str, base_url: str) -> dict[str, str]:
    """Return headers for asset download without leaking API credentials.

    Meshy model URLs are usually signed asset/CDN URLs. The create/poll API
    calls use Authorization, but downloads should not echo that bearer token
    to arbitrary hosts returned in model_urls.
    """
    parsed = urlparse(url)
    base = urlparse(base_url)
    if parsed.scheme and parsed.netloc and parsed.netloc != base.netloc:
        return {}
    return {}
