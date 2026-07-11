from __future__ import annotations

from collections.abc import Callable

from modules.newbie_request.fixtures import (
    bear_keyring_assembly_spec,
    rabbit_keyring_assembly_spec,
)
from modules.newbie_request.schemas import AssemblySpec

_ASSEMBLY_FACTORIES: dict[str, Callable[[], AssemblySpec]] = {
    "bear_keyring": bear_keyring_assembly_spec,
    "rabbit_keyring": rabbit_keyring_assembly_spec,
}


def assembly_spec_from_request_id(request_id: str) -> AssemblySpec:
    try:
        factory = _ASSEMBLY_FACTORIES[request_id]
    except KeyError as exc:
        raise ValueError(f"No AssemblySpec factory registered for request_id={request_id}") from exc
    return factory()


def supported_assembly_request_ids() -> tuple[str, ...]:
    return tuple(sorted(_ASSEMBLY_FACTORIES))


__all__ = ["assembly_spec_from_request_id", "supported_assembly_request_ids"]
