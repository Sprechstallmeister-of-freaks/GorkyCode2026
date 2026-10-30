from assistant_tools.search.lexical import LexicalEventSearch
from assistant_tools.search.models import SearchHit


class FakeRetriever:
    def __init__(self, by_query):
        self.by_query = by_query
        self.queries = []

    def search(self, query, filters, limit):
        self.queries.append(query)
        return self.by_query.get(query, [])[:limit]


def test_first_search_uses_only_original_query():
    event = {"id": "1", "title": "Подготовка доклада"}
    retriever = FakeRetriever({"что я планировал для выступления": [SearchHit("1", event)]})
    search = LexicalEventSearch(retriever)

    results = search.search(
        query="что я планировал для выступления", filters={}, limit=10
    )

    assert retriever.queries == ["что я планировал для выступления"]
    assert results[0]["id"] == "1"
    assert "relevance" in results[0]


def test_retry_searches_variants_and_deduplicates_results():
    shared = {"id": "shared", "title": "Подготовка доклада"}
    other = {"id": "other", "title": "Презентация"}
    retriever = FakeRetriever({
        "подготовка выступления": [SearchHit("shared", shared), SearchHit("other", other)],
        "доклад на конференции": [SearchHit("shared", shared)],
    })
    search = LexicalEventSearch(retriever)

    results = search.search(
        query="что я планировал для выступления",
        query_variants=["подготовка выступления", "доклад на конференции"],
        filters={},
        limit=10,
    )

    assert retriever.queries == ["подготовка выступления", "доклад на конференции"]
    assert [item["id"] for item in results] == ["shared", "other"]


def test_empty_first_search_returns_empty_without_expansion():
    retriever = FakeRetriever({})
    search = LexicalEventSearch(retriever)

    results = search.search(query="несуществующая запись", filters={}, limit=10)

    assert results == []
    assert retriever.queries == ["несуществующая запись"]
