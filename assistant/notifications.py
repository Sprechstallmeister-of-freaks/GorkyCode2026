"""Выбор событий, время наступления которых уже наступило, и пометка их уведомлёнными.

Логика полностью серверная: по UTC-времени «сейчас» находятся открытые события с
указанной датой/временем, ещё не уведомлённые (notified_at IS NULL), чьё локальное
время (с учётом часового пояса события) наступило не позднее now + tolerance.
Пометка и выбор делаются в одной транзакции BEGIN IMMEDIATE, чтобы при частых
опросах из браузера уведомление уходило один раз.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from .database import Database
from .models import Event
from .service import Planner, DomainError

logger = logging.getLogger("assistant.notifications")


class NotificationSettings:
    """Параметры отбора событий к уведомлению."""

    def __init__(
        self,
        *,
        max_age_hours: int = 24,
        tolerance_seconds: int = 60,
    ) -> None:
        self.max_age = timedelta(hours=max_age_hours)
        self.tolerance = timedelta(seconds=tolerance_seconds)


def due_events(
    planner: Planner,
    db: Database,
    *,
    now_utc: datetime,
    settings: NotificationSettings | None = None,
) -> list[Event]:
    """Вернуть события, время которых наступило, и пометить их уведомлёнными.

    Выбор и пометка происходят атомарно в рамках одной транзакции.
    """
    settings = settings or NotificationSettings()
    now_utc = now_utc.astimezone(timezone.utc)
    oldest = now_utc - settings.max_age
    deadline = now_utc + settings.tolerance

    due: list[Event] = []
    now_iso = now_utc.isoformat()

    with db.transaction() as cx:
        rows = cx.execute(
            """SELECT * FROM events
               WHERE status='open'
                 AND scheduled_date IS NOT NULL
                 AND scheduled_time IS NOT NULL
                 AND notified_at IS NULL""",
        ).fetchall()

        to_mark: list[int] = []
        for row in rows:
            event_date = row["scheduled_date"]
            event_time = row["scheduled_time"]
            tz_name = row["timezone"]
            try:
                event_utc = planner._utc(
                    date.fromisoformat(event_date),
                    time.fromisoformat(event_time),
                    tz_name,
                )
            except (DomainError, ValueError) as exc:
                logger.warning(
                    "Пропуск события при расчёте уведомления",
                    extra={
                        "event_id": row["id"],
                        "action": "notification_skip",
                        "error": str(exc),
                    },
                )
                continue
            if oldest < event_utc <= deadline:
                to_mark.append(row["id"])
                event = planner._row(row)
                event.notified_at = now_utc
                due.append(event)

        if to_mark:
            cx.executemany(
                "UPDATE events SET notified_at=? WHERE id=?",
                [(now_iso, eid) for eid in to_mark],
            )
            logger.info(
                "События помечены уведомлёнными",
                extra={
                    "action": "notifications_marked",
                    "event_ids": to_mark,
                    "count": len(to_mark),
                    "now_utc": now_iso,
                },
            )

    return due
