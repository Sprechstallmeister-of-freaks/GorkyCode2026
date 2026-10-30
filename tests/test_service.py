from datetime import date, time
import pytest
from pydantic import ValidationError
from assistant.models import EventCreate, EventPatch, Kind
from assistant.service import ConflictError, DomainError, NotFoundError


def create_meeting(planner, title='Команда', day=date(2026,11,5), clock=time(15,0), duration=60):
    return planner.create(EventCreate(kind=Kind.meeting,title=title,scheduled_date=day,
                                      scheduled_time=clock,duration_minutes=duration))


def test_create_and_get_and_persistence(tmp_path):
    from assistant.database import Database
    from assistant.service import Planner
    p1 = Planner(Database(tmp_path/'repeat.db'), 'Europe/Moscow')
    event = p1.create(EventCreate(kind='task', title='  Отправить   отчёт  ', scheduled_date='2026-11-05'))
    assert event.id > 0 and event.title == 'Отправить отчёт'
    assert event.status == 'open' and event.version == 1
    p2 = Planner(Database(tmp_path/'repeat.db'), 'Europe/Moscow')
    assert p2.get(event.id).title == 'Отправить отчёт'


def test_validation_requires_date_and_time_for_meetings_and_reminders():
    for kind in ('meeting','reminder'):
        with pytest.raises(ValidationError): EventCreate(kind=kind, title='Тест',scheduled_date='2026-11-05')
    with pytest.raises(ValidationError): EventCreate(kind='task',title='Тест',scheduled_time='15:00')
    with pytest.raises(ValidationError): EventCreate(kind='task', title='    ')


def test_conflicts_and_boundary(planner):
    a = create_meeting(planner)
    with pytest.raises(ConflictError): create_meeting(planner,'Конфликт',clock=time(15,30))
    b = create_meeting(planner,'Следом',clock=time(16,0))
    assert b.id != a.id
    with pytest.raises(ConflictError):
        planner.update(b.id, EventPatch(scheduled_time=time(15,45)))
    assert planner.get(b.id).scheduled_time == time(16,0) # transaction rollback


def test_cancel_releases_slot_and_soft_delete(planner):
    a = create_meeting(planner)
    cancelled = planner.cancel(a.id)
    assert cancelled.status == 'cancelled'
    assert not planner.list(from_date=date(2026,11,5),to_date=date(2026,11,5))[0]
    assert planner.get(a.id).status == 'cancelled'
    create_meeting(planner,'Замена')


def test_cross_midnight_conflict(planner):
    create_meeting(planner,'Ночная',day=date(2026,11,5),clock=time(23,30),duration=90)
    with pytest.raises(ConflictError):
        create_meeting(planner,'Слишком рано',day=date(2026,11,6),clock=time(0,30))


def test_date_only_and_interval_inclusive(planner):
    planner.create(EventCreate(kind='task',title='Без даты'))
    planner.create(EventCreate(kind='task',title='Понедельник',scheduled_date='2026-11-02'))
    planner.create(EventCreate(kind='task',title='Вторник',scheduled_date='2026-11-03'))
    rows, total = planner.list(from_date=date(2026,11,2),to_date=date(2026,11,2))
    assert [e.title for e in rows] == ['Понедельник'] and total == 1
    assert planner.list()[1] == 3
    with pytest.raises(DomainError): planner.list(from_date=date(2026,11,5),to_date=date(2026,11,2))


def test_version_prevents_lost_updates(planner):
    a=planner.create(EventCreate(kind='task',title='Отчёт'))
    b=planner.update(a.id, EventPatch(title='Отчёт готов',expected_version=1))
    assert b.version==2
    with pytest.raises(ConflictError): planner.update(a.id,EventPatch(title='Неактуальное',expected_version=1))
    assert planner.get(a.id).title == 'Отчёт готов'


def test_search_inflection_and_misspelling(planner):
    planner.create(EventCreate(kind='task',title='Отправить договор Ивану'))
    planner.create(EventCreate(kind='task',title='Проверить презентацию'))
    matches,_=planner.search('задача по договору')
    assert matches and matches[0].title=='Отправить договор Ивану'
    matches,_=planner.search('презентации')
    assert matches and matches[0].title=='Проверить презентацию'
    assert not planner.search('космический корабль')[0]


def test_import_idempotent_with_source_ids(planner, tmp_path):
    p=tmp_path/'small.csv'
    p.write_text('id,date,time,text\n1,2024-01-01,10:00,Задача\n2,2024-01-02,12:30,Встреча по проекту\n',encoding='utf-8')
    assert planner.import_csv(p)==2
    assert planner.import_csv(p)==0
    records,_=planner.list()
    assert len(records)==2 and all(e.source=='csv' and e.kind=='task' for e in records)
    assert planner.create(EventCreate(kind='task',title='Новая')).id not in (0,)
    assert planner.list()[1]==3


def test_import_rejects_invalid_file_atomically(planner, tmp_path):
    p=tmp_path/'bad.csv';p.write_text('id,date,time,text\n1,2025-01-01,13:00,Ок\n2,invalid,14:00,Ошибка\n',encoding='utf-8')
    with pytest.raises(DomainError): planner.import_csv(p)
    assert planner.list()[1]==0


def test_dst_gap_rejected(planner):
    with pytest.raises(DomainError):
        planner.create(EventCreate(kind='meeting',title='DST',scheduled_date='2026-03-29',scheduled_time='01:30',timezone='Europe/London'))


def test_history_and_sessions_persist(planner):
    e=planner.create(EventCreate(kind='task',title='Договор'))
    planner.set_session('hello',active_event_id=e.id, pending_action='create_event',pending_data={'a':1})
    planner.append_dialogue('hello','Найди договор','Нашёл')
    assert planner.get_session('hello')['active_event_id']==e.id
    assert planner.get_session('hello')['pending_data']=={'a':1}
    assert [m['role'] for m in planner.history('hello')]==['user','assistant']


def test_not_found(planner):
    with pytest.raises(NotFoundError): planner.get(999)
