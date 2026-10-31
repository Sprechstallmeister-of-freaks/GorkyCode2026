from __future__ import annotations

import copy
import json
from datetime import date, time

import httpx
import pytest
from fastapi.testclient import TestClient

from assistant.api import create_app
from assistant.config import Settings
from assistant.deepseek import DeepSeekClient, DeepSeekError
from assistant.deepseek_provider import DeepSeekProvider
from assistant.models import EventCreate
from assistant.routing import ChatRouter


class FakeDeepSeek:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.connected = False
        self.closed = False

    def complete(self, messages, tools):
        self.calls.append({"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools)})
        if not self.responses:
            raise AssertionError("Unexpected extra DeepSeek call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            self.connected = False
            raise response
        self.connected = True
        return response

    def close(self):
        self.closed = True


def text(content):
    return {"role": "assistant", "content": content}


def call(name, args, call_id="call_1"):
    raw_args = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": raw_args}}
    ]}


def router(planner, client):
    return ChatRouter(planner, provider=DeepSeekProvider(client))


def test_deepseek_create_uses_tool_and_server_confirmation(planner):
    client = FakeDeepSeek(call("create_event", {
        "kind": "meeting", "title": "Встреча с командой",
        "scheduled_date": "2026-10-31", "scheduled_time": "15:00",
    }))
    result = router(planner, client).process("Собери команду завтра в три", "create")

    assert result.action == "create_event" and result.mode == "deepseek"
    assert result.event.title == "Встреча с командой"
    assert "Создано:" in result.answer
    assert planner.list()[1] == 1
    assert len(client.calls) == 1
    assert len(client.calls[0]["tools"]) == 6
    assert "Europe/Moscow" in client.calls[0]["messages"][0]["content"]
    assert len(planner.history("create")) == 2


def test_deepseek_describes_actual_list_result(planner):
    planner.create(EventCreate(kind="task", title="Отправить отчёт", scheduled_date=date(2026, 11, 2)))
    client = FakeDeepSeek(
        call("list_events", {"from_date": "2026-11-02", "to_date": "2026-11-02"}),
        text("В понедельник нужно отправить отчёт."),
    )
    result = router(planner, client).process("Сориентируй по делам на 2026-11-02", "list")

    assert result.answer == "В понедельник нужно отправить отчёт."
    assert result.action == "list_events" and result.mode == "deepseek"
    assert [event.title for event in result.events] == ["Отправить отчёт"]
    assert len(client.calls) == 2
    assert client.calls[1]["tools"] == []
    assert "Отправить отчёт" in client.calls[1]["messages"][-1]["content"]


def test_rule_search_can_use_hybrid_description(planner):
    planner.create(EventCreate(kind="task", title="Проверить договор"))
    client = FakeDeepSeek(text("Нашёл задачу: проверить договор."))
    result = router(planner, client).process("Найди задачу про договор", "rule-read")

    assert result.action == "search" and result.mode == "deepseek"
    assert result.answer == "Нашёл задачу: проверить договор."
    assert len(client.calls) == 1
    assert client.calls[0]["tools"] == []


def test_read_result_is_kept_when_description_fails(planner):
    planner.create(EventCreate(kind="task", title="Проверить договор"))
    client = FakeDeepSeek(
        call("search_events", {"query": "договор"}),
        DeepSeekError("timeout"),
    )
    result = router(planner, client).process("Откопай упоминание договора", "read-fallback")

    assert result.action == "search_events"
    assert "Проверить договор" in result.answer
    assert len(result.events) == 1
    assert len(planner.history("read-fallback")) == 2


def test_recognized_rule_bypasses_deepseek(planner):
    client = FakeDeepSeek()
    result = router(planner, client).process("Напомни мне завтра в 10 утра позвонить Ивану", "rules")

    assert result.action == "create" and result.mode == "fallback"
    assert planner.list()[1] == 1
    assert client.calls == []


def test_malformed_tool_json_never_reaches_database(planner):
    client = FakeDeepSeek(call("create_event", '{"kind":'))
    result = router(planner, client).process("Собери команду завтра в три", "bad-json")

    assert result.action == "unrecognized" and result.mode == "fallback"
    assert planner.list()[1] == 0


def test_provider_failure_has_controlled_response(planner):
    client = FakeDeepSeek(DeepSeekError("offline"))
    result = router(planner, client).process("Совершенно нестандартная просьба", "offline")

    assert result.action == "unrecognized" and result.needs_llm
    assert planner.list()[1] == 0
    assert client.connected is False


def test_target_query_is_resolved_by_server_before_update(planner):
    event = planner.create(EventCreate(kind="task", title="Подготовить договор"))
    client = FakeDeepSeek(call("update_event", {"status": "done", "target_query": "договор"}))
    result = router(planner, client).process("Закрой договорную работу", "update")

    assert result.action == "update_event" and result.mode == "deepseek"
    assert planner.get(event.id).status.value == "done"


def test_get_event_result_can_be_described(planner):
    event = planner.create(EventCreate(kind="task", title="Позвонить Ивану"))
    client = FakeDeepSeek(
        call("get_event", {"event_id": event.id}),
        text(f"Задача #{event.id}: позвонить Ивану."),
    )
    result = router(planner, client).process("Расскажи подробнее о записи", "get")

    assert result.action == "get_event" and result.event.id == event.id
    assert result.answer == f"Задача #{event.id}: позвонить Ивану."


def test_cancel_requires_separate_user_confirmation(planner):
    event = planner.create(EventCreate(kind="task", title="Старый черновик"))
    client = FakeDeepSeek(call("cancel_event", {"event_id": event.id, "expected_version": 1}))
    chat = router(planner, client)

    question = chat.process("Избавься от этой записи", "cancel")
    assert question.action == "clarify" and question.mode == "deepseek"
    assert planner.get(event.id).status.value == "open"
    assert planner.get_session("cancel")["pending_action"] == "confirm_cancel"

    confirmed = chat.process("да", "cancel")
    assert confirmed.action == "cancel_event"
    assert planner.get(event.id).status.value == "cancelled"
    assert planner.get_session("cancel")["pending_action"] is None
    assert len(client.calls) == 1


def test_business_conflict_is_not_retried(planner):
    planner.create(EventCreate(kind="meeting", title="Первая", scheduled_date=date(2026, 11, 5), scheduled_time=time(15, 0)))
    client = FakeDeepSeek(call("create_event", {
        "kind": "meeting", "title": "Вторая", "scheduled_date": "2026-11-05", "scheduled_time": "15:30",
    }))
    result = router(planner, client).process("Забронируй получасовой созвон", "conflict")

    assert result.action == "conflict" and result.mode == "deepseek"
    assert "пересекается" in result.answer
    assert planner.list()[1] == 1
    assert len(client.calls) == 1


def test_missing_fields_use_server_clarification(planner):
    client = FakeDeepSeek(call("create_event", {"kind": "meeting", "title": "Созвон с Иваном"}))
    chat = router(planner, client)
    first = chat.process("Хочу созвониться с Иваном", "clarify")

    assert first.action == "clarify" and first.mode == "deepseek"
    assert chat.process("2026-11-04", "clarify").action == "clarify"
    done = chat.process("15:00", "clarify")
    assert done.action == "create_event"
    assert done.event.scheduled_time.strftime("%H:%M") == "15:00"
    assert len(client.calls) == 1


def test_search_expansion_and_hybrid_answer(planner):
    planner.create(EventCreate(kind="task", title="Обсуждение зарплаты с руководителем"))
    client = FakeDeepSeek(
        call("search_events", {"query": "повышение"}),
        text('["зарплата", "оплата труда"]'),
        text("Нашёл обсуждение зарплаты с руководителем."),
    )
    result = router(planner, client).process("Вспомни, когда я говорил о повышении оплаты", "expand")

    assert result.events and result.mode == "deepseek"
    assert result.answer == "Нашёл обсуждение зарплаты с руководителем."
    assert len(client.calls) == 3


def test_api_reports_configured_and_connected_states(tmp_path):
    fake = FakeDeepSeek(call("create_event", {"kind": "task", "title": "Из модели"}))
    app = create_app(Settings(db_path=tmp_path / "api.db", seed_csv=None, import_seed=False),
                     deepseek_client=fake)
    with TestClient(app) as client:
        before = client.get("/api/health").json()
        assert before["llm_configured"] is True and before["llm_connected"] is False
        answer = client.post("/api/chat", json={"session_id": "api", "message": "Нестандартная просьба"}).json()
        assert answer["mode"] == "deepseek" and answer["action"] == "create_event"
        after = client.get("/api/health").json()
        assert after["llm_connected"] is True and after["llm_model"] == "deepseek-chat"
    assert fake.closed


def test_http_client_sends_openai_compatible_tool_request():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    client = DeepSeekClient(api_key="test-secret", base_url="https://api.deepseek.com/v1",
                            transport=httpx.MockTransport(handler))
    try:
        result = client.complete([{"role": "user", "content": "test"}], [{"type": "function"}])
    finally:
        client.close()

    assert result["content"] == "ok" and client.connected
    assert seen["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert seen["authorization"] == "Bearer test-secret"
    assert seen["payload"]["model"] == "deepseek-chat"
    assert seen["payload"]["tool_choice"] == "auto"


@pytest.mark.parametrize("status", [401, 429, 500])
def test_http_errors_have_safe_deepseek_exception(status):
    transport = httpx.MockTransport(lambda _request: httpx.Response(status, json={"error": "secret details"}))
    client = DeepSeekClient(api_key="test-secret", transport=transport)
    try:
        with pytest.raises(DeepSeekError) as error:
            client.complete([], [])
        assert str(status) in str(error.value)
        assert "secret details" not in str(error.value)
        assert client.connected is False
    finally:
        client.close()
