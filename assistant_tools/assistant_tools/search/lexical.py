from __future__ import annotations

from .models import SearchFilters, SearchHit
from .ports import Retriever


class LexicalEventSearch:
    """Runs lexical search for the original query or LLM-provided alternatives.

    The first tool call should pass no query_variants. If it returns no results,
    the LLM may call search_events again with up to five alternative phrases.
    This service never calls an LLM itself.
    """

    def __init__(self, lexical: Retriever, *, rrf_k: int = 60) -> None:
        if rrf_k < 1:
            raise ValueError("rrf_k должен быть положительным.")
        self.lexical = lexical
        self.rrf_k = rrf_k

    def search(
        self,
        *,
        query: str,
        filters: dict,
        limit: int,
        query_variants: list[str] | None = None,
    ) -> list[dict]:
        normalized_filters = SearchFilters.from_mapping(filters)
        candidate_limit = min(max(limit * 3, limit), 300)

        # On the first call, search only the user's original wording.
        # On a retry, search only the alternatives generated after an empty result.
        queries = query_variants if query_variants else [query]
        rankings = [
            self.lexical.search(item, normalized_filters, candidate_limit)
            for item in queries
        ]
        rankings = [ranking for ranking in rankings if ranking]
        if not rankings:
            return []

        # RRF combines rankings from several lexical query variants.
        # This is not semantic/embedding search.
        fused: dict[str, tuple[float, dict]] = {}
        for ranking in rankings:
            seen_in_ranking: set[str] = set()
            for rank, hit in enumerate(ranking, start=1):
                if hit.event_id in seen_in_ranking:
                    continue
                seen_in_ranking.add(hit.event_id)
                score, _ = fused.get(hit.event_id, (0.0, hit.event))
                fused[hit.event_id] = (score + 1.0 / (self.rrf_k + rank), hit.event)

        ordered = sorted(fused.items(), key=lambda item: item[1][0], reverse=True)[:limit]
        return [
            {**event, "relevance": round(score, 8)}
            for _event_id, (score, event) in ordered
        ]
