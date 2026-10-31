from datetime import datetime, timedelta, timezone

import pytest
from zoneinfo import ZoneInfo

from assistant.database import Database
from assistant.models import EventCreate, Kind
from assistant.notifications import NotificationSettings, due_events
from assistant.service import Planner

MSK = ZoneInfo("Europe/Moscow")
UTC = timezone.utc

NOW_UTC = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)  # 13:00 по Москве


def make_planner(tmp_path):
    return Planner(Database(tmp_path / "test.db"), "Europe/Moscow")


def test_due_event_returned_once_and_marked(tmp_path):
    planner = make_planner(tmp_path)
    # 09:00 мск = 06:00 UTC — уже наступило к 10:00 UTC.
    event = planner.create(
        EventCreate(
            kind="reminder",
            title="Позвонить Ивану",
            scheduled_date="2026-10-30",
            scheduled_time="09:00",
        )
    )

    first = due_events(planner, planner.db, now_utc=NOW_UTC)
    assert [e.id for e in first] == [event.id]
    assert first[0].notified_at is not None

    second = due_events(planner, planner.db, now_utc=NOW_UTC)
    assert second == []


def test_future_event_not_returned(tmp_path):
    planner = make_planner(tmp_path)
    # 14:00 мск = 11:00 UTC — к 10:00 UTC ещё не наступило.
    planner.create(
        EventCreate(
            kind="reminder",
            title="Позже",
            scheduled_date="2026-10-30",
            scheduled_time="14:00",
        )
    )

    assert due_events(planner, planner.db, now_utc=NOW_UTC) == []


def test_event_in_other_timezone_respected(tmp_path):
    planner = make_planner(tmp_path)
    # 10:00 뉴욕 = 14:00 UTC — к 10:00 UTC ещё не наступило.
    planner.create(
        EventCreate(
            kind="reminder",
            title="Нью-Йорк",
            scheduled_date="2026-10-30",
            scheduled_time="10:00",
            timezone="America/New_York",
        )
    )

    assert due_events(planner, planner.db, now_utc=NOW_UTC) == []

    # Теперь сдвинем «сейчас» на 15:00 UTC — событие наступило.
    later = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
    due = due_events(planner, planner.db, now_utc=later)
    assert [e.title for e in due] == ["Нью-Йорк"]


def test_nonexistent_dst_time_skipped(tmp_path):
    # create() уже отвергает несуществующее время; здесь вставляем битую строку
    # напрямую в БД, чтобы проверить, что due_events её пропускает, не падая.
    planner = Planner(Database(tmp_path / "test.db"), "America/New_York")
    with planner.db.transaction() as cx:
        cx.execute(
            """INSERT INTO events
               (kind,title,scheduled_date,scheduled_time,duration_minutes,timezone,status,created_at,updated_at,source)
               VALUES('reminder','ДST','2026-03-08','02:30',NULL,'America/New_York','open',?,?,'manual')""",
            ("2026-03-08T04:00:00+00:00", "2026-03-08T04:00:00+00:00"),
        )

    now = datetime(2026, 3, 8, 7, 0, tzinfo=UTC)
    assert due_events(planner, planner.db, now_utc=now) == []


def test_event_without_time_not_returned(tmp_path):
    planner = make_planner(tmp_path)
    planner.create(
        EventCreate(
            kind="task",
            title="Без времени",
            scheduled_date="2026-10-30",
        )
    )

    assert due_events(planner, planner.db, now_utc=NOW_UTC) == []


def test_cancelled_event_not_returned(tmp_path):
    planner = make_planner(tmp_path)
    event = planner.create(
        EventCreate(
            kind="reminder",
            title="Отменённое",
            scheduled_date="2026-10-30",
            scheduled_time="09:00",
        )
    )
    planner.cancel(event.id)

    assert due_events(planner, planner.db, now_utc=NOW_UTC) == []


def test_editing_time_resets_notified(tmp_path):
    planner = make_planner(tmp_path)
    event = planner.create(
        EventCreate(
            kind="reminder",
            title="Первое время",
            scheduled_date="2026-10-30",
            scheduled_time="09:00",
        )
    )

    first = due_events(planner, planner.db, now_utc=NOW_UTC)
    assert len(first) == 1
    assert planner.get(event.id).notified_at is not None

    # Переносим на другое время — уведомление должно сброситься.
    from assistant.models import EventPatch

    planner.update(event.id, EventPatch(scheduled_time="12:00"))
    assert planner.get(event.id).notified_at is None

    # И снова должно прийти уведомление (новое время 12:00 мск = 09:00 UTC — в прошлом к 10:00 UTC).
    second = due_events(planner, planner.db, now_utc=NOW_UTC)
    assert [e.id for e in second] == [event.id]


def test_due_api_endpoint(client):
    # Событие на сегодня в час назад по Москве — уже наступило.
    now_msk = datetime.now(ZoneInfo("Europe/Moscow"))
    past = now_msk - timedelta(hours=2)

    client.post(
        "/api/events",
        json={
            "kind": "reminder",
            "title": "Через API",
            "scheduled_date": now_msk.date().isoformat(),
            "scheduled_time": past.strftime("%H:%M"),
        },
    )

    response = client.get("/api/notifications/due")
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["items"][0]["title"] == "Через API"

    # Повторный опрос — пусто, так как помечено уведомлённым.
    response2 = client.get("/api/notifications/due")
    assert response2.json()["count"] == 0
