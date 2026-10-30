from __future__ import annotations

from typing import Protocol

from .models import SearchFilters, SearchHit


class Retriever(Protocol):
    """Lexical search adapter, e.g. SQLite FTS5/BM25."""

    def search(self, query: str, filters: SearchFilters, limit: int) -> list[SearchHit]:
        ...
