from __future__ import annotations

from assistant_tools import (
    CreateMeetingTool,
    CreateReminderTool,
    CreateTaskTool,
    ListEventsTool,
    SearchEventsTool,
    ToolRegistry,
)


class FakeRepository:
    def __init__(self):
        self.created = []
        self.filters = None

    def create_event(self, event):
        self.created.append(event)
        return {"id": str(len(self.created)), **event}

    def list_events(self, filters):
        self.filters = filters
        return [{"id": "1", "title": "Запись"}]


class FakeSearch:
    def __init__(self):
        self.call = None

    def search(self, *, query, filters, limit, query_variants=None):
        self.call = (query, filters, limit, query_variants)
        return [{"id": "e1", "title": "Подготовка доклада", "relevance": 0.02}]


def test_create_task_accepts_unscheduled_task():
    repository = FakeRepository()
    result = CreateTaskTool(repository).invoke({
        "title": "Разобрать почту", "description": None, "due_date": None, "due_at": None
    })
    assert result.success
    assert repository.created[0]["type"] == "task"


def test_create_task_rejects_both_due_fields():
    repository = FakeRepository()
    result = CreateTaskTool(repository).invoke({
        "title": "Подготовить отчёт", "description": None,
        "due_date": "2026-11-06", "due_at": "2026-11-06T12:00:00+01:00"
    })
    assert not result.success
    assert result.error.code == "VALIDATION_ERROR"
    assert repository.created == []


def test_reminder_requires_timezone():
    repository = FakeRepository()
    result = CreateReminderTool(repository).invoke({
        "title": "Позвонить", "description": None, "remind_at": "2026-11-06T12:00:00"
    })
    assert not result.success
    assert "часовой пояс" in result.error.message


def test_meeting_end_must_be_after_start():
    repository = FakeRepository()
    result = CreateMeetingTool(repository).invoke({
        "title": "Встреча", "description": None,
        "scheduled_at": "2026-11-06T12:00:00+01:00",
        "end_at": "2026-11-06T11:00:00+01:00"
    })
    assert not result.success
    assert result.error.code == "VALIDATION_ERROR"


def test_list_events_validates_period_and_normalizes_empty_types():
    repository = FakeRepository()
    result = ListEventsTool(repository).invoke({
        "start_at": "2026-11-06T00:00:00+01:00",
        "end_at": "2026-11-07T00:00:00+01:00",
        "types": [], "status": None, "limit": 20,
    })
    assert result.success
    assert repository.filters["types"] is None


def test_search_events_passes_query_and_filters():
    search = FakeSearch()
    result = SearchEventsTool(search).invoke({
        "query": "  подготовка доклада  ", "types": ["task"], "status": "completed",
        "date_from": None, "date_to": None, "limit": 10,
    })
    assert result.success
    assert search.call[0] == "подготовка доклада"
    assert search.call[1]["types"] == ["task"]
    assert search.call[3] == []


def test_search_events_passes_query_variants_on_retry():
    search = FakeSearch()
    result = SearchEventsTool(search).invoke({
        "query": "что я планировал для выступления",
        "query_variants": ["подготовка доклада", "презентация на конференции"],
        "types": [], "status": None, "date_from": None, "date_to": None, "limit": 10,
    })
    assert result.success
    assert search.call[3] == ["подготовка доклада", "презентация на конференции"]


def test_registry_is_provider_neutral_and_dispatches():
    registry = ToolRegistry([CreateTaskTool(FakeRepository())])
    definitions = registry.definitions()
    assert definitions[0]["name"] == "create_task"
    result = registry.execute("missing_tool", {})
    assert not result.success
    assert result.error.code == "NOT_FOUND"
