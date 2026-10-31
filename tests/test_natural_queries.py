from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from assistant.contracts import ParsedCommand
from assistant.dialogue import RuleDialogue
from assistant.freeform import FreeFormParser
from assistant.models import EventCreate
from assistant.routing import ChatRouter


NOW = datetime(2026, 10, 30, 9, 44, tzinfo=ZoneInfo("Europe/Moscow"))


class NoToolProvider:
    connected = True

    def __init__(self):
        self.interpret_calls = 0

    def interpret(self, text, context, tools):
        self.interpret_calls += 1
        return ParsedCommand(tool="unknown")

    def expand_search(self, query, context):
        return []


class CreateWithoutDateProvider(NoToolProvider):
    def interpret(self, text, context, tools):
        self.interpret_calls += 1
        if "после" in text.lower():
            return ParsedCommand(tool="unknown")
        return ParsedCommand(tool="create_event", arguments={
            "kind": "task", "title": "Побегать с Аллой",
        })


def make_router(planner, with_llm=False, provider=None):
    if provider is None and with_llm:
        provider = NoToolProvider()
    rules = RuleDialogue(planner, clock=lambda: NOW)
    return ChatRouter(planner, provider=provider, rules=rules), provider


@pytest.mark.parametrize("with_llm", [False, True])
@pytest.mark.parametrize(("message", "target"), [
    ("Если я завтра проснусь с утра что нужно будет сделать?", date(2026, 10, 31)),
    ("Я хотел бы начать бегать сколько дел нужно сделать чтобы я мог послезавтра побегать?", date(2026, 11, 1)),
])
def test_agenda_questions_read_exact_day_and_never_create(planner, message, target, with_llm):
    planner.create(EventCreate(kind="task", title="Задача на нужный день",
                               scheduled_date=target, scheduled_time="08:00"))
    planner.create(EventCreate(kind="meeting", title="Встреча на нужный день",
                               scheduled_date=target, scheduled_time="12:00"))
    planner.create(EventCreate(kind="reminder", title="Напоминание на нужный день",
                               scheduled_date=target, scheduled_time="18:00"))
    planner.create(EventCreate(kind="task", title="Дело на другой день",
                               scheduled_date=target.fromordinal(target.toordinal() + 1),
                               scheduled_time="10:00"))
    before = planner.list()[1]
    chat, provider = make_router(planner, with_llm=with_llm)

    result = chat.process(message, "agenda")

    assert result.action == "list"
    assert len(result.events) == 3
    assert {event.kind.value for event in result.events} == {"task", "meeting", "reminder"}
    assert {event.scheduled_date for event in result.events} == {target}
    assert "Дело на другой день" not in result.answer
    assert "Создано" not in result.answer
    assert planner.list()[1] == before
    if provider is not None:
        assert provider.interpret_calls == 1


@pytest.mark.parametrize("with_llm", [False, True])
def test_after_named_event_uses_exact_anchor_and_only_later_events(planner, with_llm):
    anchor = planner.create(EventCreate(
        kind="task", title="Созвон с Орбитой по презентации",
        scheduled_date="2026-10-29", scheduled_time="15:30",
    ))
    planner.create(EventCreate(kind="task", title="Созвон с Орбитой по интеграции",
                               scheduled_date="2027-02-02", scheduled_time="10:45"))
    planner.create(EventCreate(kind="task", title="Созвон с Альфой по презентации",
                               scheduled_date="2026-11-01", scheduled_time="15:15"))
    planner.create(EventCreate(kind="task", title="Раньше созвона",
                               scheduled_date="2026-10-29", scheduled_time="14:00"))
    planner.create(EventCreate(kind="task", title="Одновременно с созвоном",
                               scheduled_date="2026-10-29", scheduled_time="15:30"))
    later_task = planner.create(EventCreate(kind="task", title="Отправить итоги",
                                            scheduled_date="2026-10-29", scheduled_time="16:15"))
    later_reminder = planner.create(EventCreate(kind="reminder", title="Проверить цифры",
                                                scheduled_date="2026-10-29", scheduled_time="18:00"))
    planner.create(EventCreate(kind="task", title="На следующий день",
                               scheduled_date="2026-10-30", scheduled_time="09:00"))
    before = planner.list()[1]
    chat, provider = make_router(planner, with_llm=with_llm)

    result = chat.process(
        "Созвон с Орбитой по презентации после нее есть что-то в тот день?",
        "after",
    )

    assert result.action == "after_event"
    assert result.event.id == anchor.id
    assert [event.id for event in result.events] == [later_task.id, later_reminder.id]
    assert "Созвон с Орбитой по интеграции" not in result.answer
    assert "Созвон с Альфой по презентации" not in result.answer
    assert "И ещё" not in result.answer
    assert planner.list()[1] == before
    if provider is not None:
        assert provider.interpret_calls == 1


def test_after_named_event_answers_empty_instead_of_dumping_fuzzy_matches(planner):
    anchor = planner.create(EventCreate(
        kind="task", title="Созвон с Орбитой по презентации",
        scheduled_date="2026-10-29", scheduled_time="15:30",
    ))
    planner.create(EventCreate(kind="task", title="Другое дело в то же время",
                               scheduled_date="2026-10-29", scheduled_time="15:30"))
    for index in range(5):
        planner.create(EventCreate(kind="task", title=f"Созвон с Орбитой по проекту {index}",
                                   scheduled_date="2026-11-01", scheduled_time="10:00"))
    chat, _ = make_router(planner)

    result = chat.process(
        "Созвон с Орбитой по презентации после нее есть что-то в тот день?",
        "empty-after",
    )

    assert result.action == "after_event"
    assert result.event.id == anchor.id
    assert result.events == []
    assert "ничего не запланировано" in result.answer
    assert "Созвон с Орбитой по проекту" not in result.answer


def test_ambiguous_after_event_selection_resumes_original_read(planner):
    planner.create(EventCreate(kind="task", title="Созвон с Орбитой",
                               scheduled_date="2026-10-29", scheduled_time="15:30"))
    second = planner.create(EventCreate(kind="task", title="Созвон с Орбитой",
                                        scheduled_date="2026-10-30", scheduled_time="15:30"))
    later = planner.create(EventCreate(kind="task", title="Подвести итоги",
                                       scheduled_date="2026-10-30", scheduled_time="16:00"))
    chat, _ = make_router(planner)

    clarification = chat.process("Созвон с Орбитой после него что есть в тот день?", "select-after")
    assert clarification.action == "clarify"
    assert len(clarification.events) == 2
    assert planner.get_session("select-after")["pending_action"] == "select_after_event"

    result = chat.process("вторую", "select-after")
    assert result.action == "after_event"
    assert result.event.id == second.id
    assert [event.id for event in result.events] == [later.id]


@pytest.mark.parametrize("with_llm", [False, True])
def test_create_on_this_day_uses_date_from_previous_event(planner, with_llm):
    anchor = planner.create(EventCreate(
        kind="task", title="Созвон с Орбитой по аналитике",
        scheduled_date="2026-05-14", scheduled_time="18:00",
    ))
    provider = CreateWithoutDateProvider() if with_llm else None
    chat, _ = make_router(planner, provider=provider)

    previous = chat.process("Созвон с Орбитой по аналитике после нее что-то есть?", "context-date")
    assert previous.action == "after_event"
    assert previous.event.id == anchor.id

    result = chat.process("Запиши на этот день что я хотел бы побегать с аллой", "context-date")
    created = result.event or result.events[0]
    assert result.action in {"create", "create_event"}
    assert created.scheduled_date == date(2026, 5, 14)
    assert created.scheduled_date != NOW.date()
    assert "14.05.2026" in result.answer
    assert planner.list()[1] == 2
    if provider is not None:
        assert provider.interpret_calls == 2


def test_explicit_task_statement_still_creates(planner):
    chat, _ = make_router(planner, with_llm=True)
    result = chat.process("В пятницу нужно отправить отчет", "create-control")
    assert result.action == "create"
    assert result.event.title == "отправить отчет"
    assert planner.list()[1] == 1


def test_freeform_task_without_date_is_created_once(planner):
    chat, _ = make_router(planner)
    result = chat.process("Записать купить молоко", "without-date")
    assert result.action == "create"
    assert len(result.events) == 1
    assert result.events[0].scheduled_date is None
    assert planner.list()[1] == 1


@pytest.mark.parametrize(("message", "target"), [
    ("Если я завтра проснусь с утра что нужно будет сделать?", date(2026, 10, 31)),
    ("Я хотел бы начать бегать сколько дел нужно сделать чтобы я мог послезавтра побегать?", date(2026, 11, 1)),
])
def test_freeform_agenda_has_closed_range_and_no_default_kind(message, target):
    parsed = FreeFormParser("Europe/Moscow").parse(message, now=NOW)
    assert parsed.is_query
    assert parsed.start_date == target
    assert parsed.end_date == target
    assert parsed.kind is None
