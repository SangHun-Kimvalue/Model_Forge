from __future__ import annotations

from modules.newbie_request.schemas import (
    NewbieCatalogEntry,
    NewbieRequestCatalog,
    NewbieRoute,
    RouteSelectionReason,
    RouteSelectionRequest,
    RouteSelectionResult,
)


class NewbieRouteSelector:
    """Deterministic route selector for catalog-backed beginner requests."""

    def __init__(
        self,
        catalog: NewbieRequestCatalog,
        *,
        unknown_route: NewbieRoute = NewbieRoute.ASK_USER,
        unknown_fallback_route: NewbieRoute = NewbieRoute.MANUAL_REVIEW,
    ) -> None:
        self._catalog = catalog
        self._by_id = catalog.by_id()
        self._by_prompt = {
            _normalize_prompt(entry.user_prompt_ko): entry
            for entry in catalog.entries
        }
        self._unknown_route = unknown_route
        self._unknown_fallback_route = unknown_fallback_route

    def select(self, request: RouteSelectionRequest) -> RouteSelectionResult:
        if request.request_id is not None and request.request_id in self._by_id:
            entry = self._by_id[request.request_id]
            return _result_from_entry(entry, RouteSelectionReason.EXACT_REQUEST_ID)

        if request.user_prompt_ko is not None:
            prompt_entry = self._by_prompt.get(_normalize_prompt(request.user_prompt_ko))
            if prompt_entry is not None:
                return _result_from_entry(
                    prompt_entry,
                    RouteSelectionReason.EXACT_USER_PROMPT,
                )

        if request.category is not None:
            for entry in self._catalog.entries:
                if entry.category == request.category:
                    return _result_from_entry(entry, RouteSelectionReason.CATEGORY_FALLBACK)

        return RouteSelectionResult(
            selected_route=self._unknown_route,
            fallback_route=self._unknown_fallback_route,
            reason=RouteSelectionReason.UNKNOWN_REQUEST,
            request_id=request.request_id,
            category=request.category,
            object_type=request.object_type,
        )


def _normalize_prompt(prompt: str) -> str:
    return "".join(char for char in prompt.casefold() if char.isalnum())


def _result_from_entry(
    entry: NewbieCatalogEntry,
    reason: RouteSelectionReason,
) -> RouteSelectionResult:
    return RouteSelectionResult(
        selected_route=entry.default_route,
        fallback_route=entry.fallback_route,
        reason=reason,
        request_id=entry.request_id,
        category=entry.category,
        object_type=entry.object_type,
        required_features=entry.required_features,
        visual_quality_required=entry.visual_quality_required,
    )


__all__ = ["NewbieRouteSelector"]
