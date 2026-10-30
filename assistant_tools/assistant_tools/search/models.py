from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SearchHit:
    event_id: str
    event: dict[str, Any]
    score: float = 0.0


@dataclass(frozen=True, slots=True)
class SearchFilters:
    types: tuple[str, ...] | None = None
    status: str | None = None
    date_from: str | None = None
    date_to: str | None = None

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "SearchFilters":
        types = values.get("types")
        return cls(
            types=tuple(types) if types else None,
            status=values.get("status"),
            date_from=values.get("date_from"),
            date_to=values.get("date_to"),
        )
