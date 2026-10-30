from __future__ import annotations

from typing import Any, Protocol


class EventRepository(Protocol):
    """Adapter implemented by the application/storage layer.

    The repository is the source of truth. It must preserve created_at separately
    from the event's due/scheduled/reminder time.
    """

    def create_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Persist and return the created event, including its stable id."""
        ...

    def list_events(self, filters: dict[str, Any]) -> list[dict[str, Any]]:
        """Return up to filters['limit'] events in [start_at, end_at)."""
        ...


class EventSearchService(Protocol):
    def search(
        self,
        *,
        query: str,
        filters: dict[str, Any],
        limit: int,
        query_variants: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return ranked events; query_variants are used only on a retry after zero hits."""
        ...
