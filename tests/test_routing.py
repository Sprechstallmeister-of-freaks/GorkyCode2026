from datetime import date
from assistant.api import create_app
from assistant.config import Settings
from assistant.contracts import ParsedCommand
from assistant.models import EventCreate
from assistant.routing import ChatRouter
from fastapi.testclient import TestClient


class FakeProvider:
    def __init__(self, result=None, synonyms=None):
        self.result = result or ParsedCommand(tool="unknown")
        self.synonyms = synonyms or []
        self.interpret_calls = 0
        self.search_calls = 0

    def interpret(self, text, context, tools):
        self.interpret_calls += 1
        assert set(context.model_dump(mode="json")) == {"now", "timezone"}
        assert "+" in context.model_dump(mode="json")["now"] or "-" in context.model_dump(mode="json")["now"][10:]
        assert len(tools) == 6
        return self.result

    def expand_search(self, query, context):
        self.search_calls += 1
        return self.synonyms


def test_known_rule_skips_llm_and_single_history(planner):
    fake = FakeProvider()
    r = ChatRouter(planner, provider=fake)
    out = r.process("Добавь встречу с отделом завтра в 11:00", "session_a")
    assert out.action == "create"
    assert fake.interpret_calls == 0
    assert len(planner.history("session_a")) == 2


def test_llm_fallback_validated_and_written(planner):
    fake = FakeProvider(ParsedCommand(tool="create_event", arguments={
        "kind": "task", "title": "Обновить документацию"}))
    r = ChatRouter(planner, provider=fake)
    out = r.process("Зафиксируй отдельным пунктом актуализацию техдоки", "session_b")
    assert out.action == "create_event"
    assert planner.get(out.event.id).title == "Обновить документацию"
    assert fake.interpret_calls == 1
    assert len(planner.history("session_b")) == 2


def test_malicious_extra_arguments_rejected(planner):
    fake = FakeProvider(ParsedCommand(tool="create_event", arguments={
        "kind": "task", "title": "Test", "raw_sql": "DROP TABLE events"}))
    result = ChatRouter(planner, provider=fake).process("Something obscure", "sid")
    assert result.action == "error"
    assert planner.list()[1] == 0


def test_llm_missing_date_time_followups(planner):
    fake = FakeProvider(ParsedCommand(tool="create_event", arguments={
        "kind": "meeting", "title": "Созвон с Иваном"}))
    r = ChatRouter(planner, provider=fake)
    first = r.process("Хочу созвониться с Иваном", "sid")
    assert first.action == "clarify"
    assert r.process("2026-11-04", "sid").action == "clarify"
    done = r.process("15:00", "sid")
    assert done.action == "create_event"
    assert done.event.scheduled_date == date(2026, 11, 4)
    assert done.event.scheduled_time.strftime('%H:%M') == "15:00"
    assert fake.interpret_calls == 1


def test_ambiguous_target_selection_no_llm_second_turn(planner):
    a = planner.create(EventCreate(kind="task", title="Документ для Ивана"))
    b = planner.create(EventCreate(kind="task", title="Документ Ивану"))
    fake = FakeProvider(ParsedCommand(tool="update_event", target_query="документ",
                                     arguments={"status": "done"}))
    r = ChatRouter(planner, provider=fake)
    response = r.process("Я выполнил документ", "sel")
    assert response.action == "clarify" and len(response.events) == 2
    assert r.process("вторую", "sel").action == "update_event"
    assert planner.get(b.id).status.value == "done"
    assert planner.get(a.id).status.value == "open"
    assert fake.interpret_calls == 1
    assert len(planner.history("sel")) == 4


def test_cancel_requires_explicit_confirmation(planner):
    event = planner.create(EventCreate(kind="task", title="Удалить протокол"))
    fake = FakeProvider(ParsedCommand(tool="cancel_event", arguments={"event_id": event.id}))
    r = ChatRouter(planner, provider=fake)
    assert r.process("Убери протокол", "cancel").action == "clarify"
    assert planner.get(event.id).status.value == "open"
    assert r.process("нет", "cancel").action == "cancel_pending"
    assert planner.get(event.id).status.value == "open"
    assert r.process("Убери протокол", "cancel").action == "clarify"
    assert r.process("да", "cancel").action == "cancel_event"
    assert planner.get(event.id).status.value == "cancelled"


def test_search_expansion_using_db(planner):
    planner.create(EventCreate(kind="task", title="Обсуждение зарплаты с руководителем"))
    fake = FakeProvider(ParsedCommand(tool="search_events", arguments={"query": "повышение"}),
                        synonyms=["зарплата с руководителем"])
    r = ChatRouter(planner, provider=fake)
    result = r.process("Вспомни, когда я говорил о повышении оплаты", "search")
    assert result.events and fake.search_calls == 1
    assert result.events[0].title == "Обсуждение зарплаты с руководителем"


def test_conflict_rejected_by_planner(planner):
    planner.create(EventCreate(kind="meeting", title="Первая", scheduled_date="2026-11-04", scheduled_time="15:00"))
    fake = FakeProvider(ParsedCommand(tool="create_event", arguments={
        "kind": "meeting", "title": "Вторая", "scheduled_date": "2026-11-04", "scheduled_time": "15:30"}))
    result = ChatRouter(planner, provider=fake).process("Забронируй получасовой созвон", "conflict")
    assert result.action == "conflict"
    assert planner.list()[1] == 1


def test_provider_failure_does_not_crash(planner):
    class Broken(FakeProvider):
        def interpret(self, *args):
            raise TimeoutError("model unavailable")
    result = ChatRouter(planner, provider=Broken()).process("Что-нибудь нестандартное", "broken")
    assert result.action == "unrecognized"
    assert len(planner.history("broken")) == 2


def test_http_injection_of_provider(tmp_path):
    fake = FakeProvider(ParsedCommand(tool="create_event", arguments={"kind": "task", "title": "Из модели"}))
    app = create_app(Settings(db_path=tmp_path / 'route.db', seed_csv=None, import_seed=False), llm_provider=fake)
    with TestClient(app) as client:
        assert client.get('/api/health').json()['llm_connected'] is True
        r = client.post('/api/chat', json={"message": "Необычная формулировка", "session_id": "http"})
        assert r.status_code == 200
        assert r.json()['action'] == 'create_event'
        assert client.get('/api/events').json()['total'] == 1
