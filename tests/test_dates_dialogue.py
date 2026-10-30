from datetime import datetime,date,time,timezone
from zoneinfo import ZoneInfo
from assistant.dates import parse_datetime,period
from assistant.dialogue import RuleDialogue

NOW=datetime(2026,10,30,9,44,tzinfo=ZoneInfo('Europe/Moscow'))


def test_dates():
    for text, expected in [('завтра',date(2026,10,31)),('послезавтра',date(2026,11,1)),('в пятницу',date(2026,10,30)),('до конца недели',date(2026,11,1)),('2026-11-03',date(2026,11,3))]:
        assert parse_datetime(text,tz='Europe/Moscow',now=NOW).day==expected
    p=parse_datetime('завтра в 10 утра',tz='Europe/Moscow',now=NOW)
    assert p.day==date(2026,10,31) and p.clock==time(10,0)
    assert parse_datetime('в четверг в 11',tz='Europe/Moscow',now=NOW).clock==time(11,0)
    assert parse_datetime('через два часа',tz='Europe/Moscow',now=NOW).clock==time(11,44)
    assert period('на следующей неделе',tz='Europe/Moscow',now=NOW)==(date(2026,11,2),date(2026,11,8))


def test_chat_create_meeting_and_clarification(planner):
    chat=RuleDialogue(planner,clock=lambda:NOW)
    a=chat.process('Добавь встречу с командой завтра','s1')
    assert a.action=='clarify'
    assert planner.list()[1]==0
    b=chat.process('в 15:00','s1')
    assert b.action=='create' and b.event.kind=='meeting' and b.event.scheduled_time==time(15,0)
    c=chat.process('Перенеси её на послезавтра','s1')
    assert c.action=='update' and c.event.scheduled_date==date(2026,11,1) and c.event.scheduled_time==time(15,0)
    assert planner.history('s1')[-1]['role']=='assistant'


def test_chat_task_and_search(planner):
    chat=RuleDialogue(planner,clock=lambda:NOW)
    a=chat.process('Напомни мне завтра в 10 утра позвонить Ивану','session')
    assert a.action=='create' and a.event.kind=='reminder'
    assert 'позвонить Ивану' in a.event.title
    b=chat.process('В пятницу нужно отправить отчет','session')
    assert b.action=='create' and b.event.kind=='task'
    result=chat.process('Что у меня сегодня?','session')
    assert result.action=='list' and any('отчет' in e.title for e in result.events)
    q=chat.process('Найди задачу про отчет','session')
    assert q.action=='search' and q.events


def test_unknown_is_explicit(planner):
    chat=RuleDialogue(planner,clock=lambda:NOW)
    response=chat.process('Обсудим стратегию продвижения?','s2')
    assert response.needs_llm and response.action=='unrecognized'
