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


def test_reminder_with_time_only_uses_nearest_future_day(planner):
    # «напоминание на 10 утра поставь» без даты не должно входить
    # в цикл уточнений: дата подставляется автоматически.
    chat=RuleDialogue(planner,clock=lambda:NOW)  # NOW = 2026-10-30 09:44
    a=chat.process('напоминание на 10 утра поставь','loop1')
    assert a.action=='create' and a.event.kind=='reminder'
    assert a.event.scheduled_date==date(2026,10,30) and a.event.scheduled_time==time(10,0)
    # Если время уже прошло — переносим на завтра.
    b=chat.process('напоминание на 8 утра поставь','loop1')
    assert b.action=='create' and b.event.scheduled_date==date(2026,10,31)


def test_pending_date_answered_with_time_does_not_loop(planner):
    # Регрессия: на вопрос о дате пользователь отвечает временем («10», «10 утра»).
    # Раньше это зацикливало уточнение; теперь событие создаётся.
    chat=RuleDialogue(planner,clock=lambda:NOW)
    planner.set_session('loop2',pending_action='create_event',pending_data={
        'kind':'reminder','title':'позвонить Ивану',
        'scheduled_date':None,'scheduled_time':'10:00'})
    result=chat.process('10 утра','loop2')
    assert result.action=='create'
    assert result.event.scheduled_date==date(2026,10,30)
    assert result.event.scheduled_time==time(10,0)


def test_pending_bare_hour_reply(planner):
    # Ответ «10» на уточнение, когда не заданы ни дата, ни время.
    chat=RuleDialogue(planner,clock=lambda:NOW)
    planner.set_session('loop3',pending_action='create_event',pending_data={
        'kind':'reminder','title':'позвонить Ивану',
        'scheduled_date':None,'scheduled_time':None})
    result=chat.process('10','loop3')
    assert result.action=='create'
    assert result.event.scheduled_date==date(2026,10,30)
    assert result.event.scheduled_time==time(10,0)


def test_after_event_query(planner):
    # «после встречи с командой завтра что-то есть?» — вопрос о расписании,
    # а не создание встречи. Должны вернуться только события ПОСЛЕ встречи.
    chat=RuleDialogue(planner,clock=lambda:NOW)
    chat.process('Добавь встречу с командой завтра','after')
    chat.process('в 15:00','after')
    chat.process('Напомни завтра в 10:00 утренний звонок','after')
    chat.process('Напомни завтра в 18:00 позвонить Ивану','after')
    r=chat.process('после встречи с командой завтра что-то есть?','after')
    assert r.action=='list'
    assert any('позвонить Ивану' in e.title for e in r.events)
    assert all(e.kind!='meeting' for e in r.events)
    assert all(e.scheduled_time and e.scheduled_time>time(15,0) for e in r.events)


def test_schedule_question_during_pending(planner):
    # Вопрос о планах во время незавершённого создания не должен
    # поглощаться уточнением («На какое время?»).
    chat=RuleDialogue(planner,clock=lambda:NOW)
    a=chat.process('Добавь встречу с командой завтра','pq')
    assert a.action=='clarify'
    r=chat.process('Что у меня сегодня?','pq')
    assert r.action=='list'
    r2=chat.process('после встречи с командой завтра что-то есть?','pq')
    assert 'На какое время' not in r2.answer
