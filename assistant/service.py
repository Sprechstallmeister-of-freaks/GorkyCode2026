from __future__ import annotations

import csv
import json
import logging
import re
import sqlite3
import unicodedata
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .database import Database
from .models import Event, EventCreate, EventPatch, Kind

logger = logging.getLogger("assistant.service")


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


class DomainError(Exception):
    pass


class Planner:
    """Business rules and atomic storage operations; independent of HTTP, speech and LLM."""

    def __init__(self, db: Database, default_timezone: str):
        self.db = db
        self.default_timezone = default_timezone

    def _zone(self, name: str) -> ZoneInfo:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise DomainError(f"Неизвестный часовой пояс: {name}") from exc

    def _utc(self, day: date, moment: time, name: str) -> datetime:
        zone = self._zone(name)
        local = datetime.combine(day, moment).replace(tzinfo=zone)
        # Reject a non-existent wall clock time during daylight-saving transition.
        if local.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
            raise DomainError("Такое местное время не существует при переходе на летнее/зимнее время")
        return local.astimezone(timezone.utc)

    @staticmethod
    def _row(row: sqlite3.Row) -> Event:
        return Event.model_validate(dict(row))

    def _conflicts(self, cx: sqlite3.Connection, *, day: date, moment: time, duration: int, zone: str, exclude: int | None = None) -> list[Event]:
        start = self._utc(day, moment, zone)
        end = start + timedelta(minutes=duration)
        # Check adjacent days as events may cross midnight. This range is safe
        # for max 24h durations even with nearby timezone offsets.
        rows = cx.execute("""SELECT * FROM events WHERE kind='meeting' AND status='open'
            AND scheduled_date BETWEEN ? AND ?""",
            ((day - timedelta(days=2)).isoformat(), (day + timedelta(days=2)).isoformat())).fetchall()
        found = []
        for row in rows:
            if row["id"] == exclude:
                continue
            other = self._utc(date.fromisoformat(row["scheduled_date"]), time.fromisoformat(row["scheduled_time"]), row["timezone"])
            other_end = other + timedelta(minutes=row["duration_minutes"] or 60)
            if start < other_end and other < end:
                found.append(self._row(row))
        return found

    def create(self, payload: EventCreate, *, reject_conflicts: bool = True) -> Event:
        zone = payload.timezone or self.default_timezone
        self._zone(zone)
        if payload.scheduled_date and payload.scheduled_time:
            self._utc(payload.scheduled_date, payload.scheduled_time, zone)
        duration = (payload.duration_minutes or 60) if payload.kind == Kind.meeting else None
        now = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as cx:
            if reject_conflicts and payload.kind == Kind.meeting:
                conflicts = self._conflicts(cx, day=payload.scheduled_date, moment=payload.scheduled_time, duration=duration, zone=zone)
                if conflicts:
                    ids = ", ".join(f"#{x.id} {x.title}" for x in conflicts[:3])
                    raise ConflictError(f"Время пересекается с другой встречей: {ids}")
            cursor = cx.execute("""INSERT INTO events
                (kind,title,scheduled_date,scheduled_time,duration_minutes,timezone,status,created_at,updated_at,source)
                VALUES(?,?,?,?,?,?,'open',?,?,'manual')""", (
                payload.kind.value, payload.title,
                payload.scheduled_date.isoformat() if payload.scheduled_date else None,
                payload.scheduled_time.strftime("%H:%M") if payload.scheduled_time else None,
                duration, zone, now, now))
            row = cx.execute("SELECT * FROM events WHERE id=?", (cursor.lastrowid,)).fetchone()
        event = self._row(row)
        logger.info(
            "Событие создано",
            extra={"event_id": event.id, "action": "create_event", "kind": event.kind,
                   "title": event.title, "timezone": event.timezone,
                   "scheduled_date": str(event.scheduled_date) if event.scheduled_date else None,
                   "scheduled_time": str(event.scheduled_time) if event.scheduled_time else None,
                   "duration_minutes": event.duration_minutes},
        )
        return event

    def get(self, event_id: int) -> Event:
        with self.db.connect() as cx:
            row = cx.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if row is None:
            logger.warning("Событие не найдено", extra={"event_id": event_id, "action": "get_event", "error": "not_found"})
            raise NotFoundError(f"Запись #{event_id} не найдена")
        event = self._row(row)
        logger.debug("Событие получено", extra={"event_id": event_id, "action": "get_event", "status": event.status})
        return event

    def list(self, *, from_date: date | None = None, to_date: date | None = None,
             kind: Kind | None = None, include_cancelled: bool = False,
             limit: int = 100, offset: int = 0) -> tuple[list[Event], int]:
        if from_date and to_date and from_date > to_date:
            raise DomainError("Дата начала позже даты окончания")
        filters: list[str] = []
        args: list[object] = []
        if from_date is not None:
            filters.append("scheduled_date >= ?")
            args.append(from_date.isoformat())
        if to_date is not None:
            filters.append("scheduled_date <= ?")
            args.append(to_date.isoformat())
        if kind:
            filters.append("kind = ?")
            args.append(kind.value)
        if not include_cancelled:
            filters.append("status != 'cancelled'")
        where = "WHERE " + " AND ".join(filters) if filters else ""
        with self.db.connect() as cx:
            total = cx.execute(f"SELECT count(*) FROM events {where}", args).fetchone()[0]
            rows = cx.execute(f"""SELECT * FROM events {where}
                ORDER BY scheduled_date IS NULL, scheduled_date, scheduled_time IS NULL, scheduled_time, id
                LIMIT ? OFFSET ?""", [*args, limit, offset]).fetchall()
        events = [self._row(r) for r in rows]
        logger.info(
            "События получены списком",
            extra={"action": "list_events", "from_date": str(from_date) if from_date else None,
                   "to_date": str(to_date) if to_date else None, "kind": kind.value if kind else None,
                   "include_cancelled": include_cancelled, "limit": limit, "offset": offset,
                   "results": len(events), "total": total},
        )
        return events, total

    @staticmethod
    def _reference_key(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value).lower().replace("ё", "е")
        return " ".join(re.sub(r"[^\w]+", " ", normalized).split())

    def resolve_reference(self, query: str, *, limit: int = 8) -> tuple[list[Event], int]:
        """Resolve a named event, preferring exact titles over fuzzy neighbours.

        Ordinary search intentionally returns broad fuzzy matches. A referential
        question such as «после неё» instead needs one precise anchor, across all
        event kinds (CSV calls are stored as tasks).
        """
        key = self._reference_key(query.strip())
        if len(key) < 2:
            raise DomainError("Для поиска нужно хотя бы 2 символа")
        with self.db.connect() as cx:
            rows = cx.execute("""SELECT * FROM events WHERE status != 'cancelled'
                ORDER BY scheduled_date IS NULL, scheduled_date,
                         scheduled_time IS NULL, scheduled_time, id""").fetchall()
        exact = [row for row in rows if self._reference_key(row["title"]) == key]
        if exact:
            return [self._row(row) for row in exact[:limit]], len(exact)
        return self.search(query, limit=limit)

    def list_after(self, event_id: int, *, limit: int = 100) -> tuple[Event, list[Event], int]:
        """Return an anchor and records starting strictly later on its local day."""
        anchor = self.get(event_id)
        if anchor.scheduled_date is None or anchor.scheduled_time is None:
            return anchor, [], 0
        args = (anchor.scheduled_date.isoformat(), anchor.scheduled_time.strftime("%H:%M"), event_id)
        where = """WHERE scheduled_date=? AND scheduled_time>? AND id!=?
                   AND status != 'cancelled'"""
        with self.db.connect() as cx:
            total = cx.execute(f"SELECT count(*) FROM events {where}", args).fetchone()[0]
            rows = cx.execute(f"""SELECT * FROM events {where}
                ORDER BY scheduled_time, id LIMIT ?""", [*args, limit]).fetchall()
        return anchor, [self._row(row) for row in rows], total

    def update(self, event_id: int, patch: EventPatch) -> Event:
        updates = patch.model_dump(exclude_unset=True, exclude={"expected_version"})
        if not updates:
            raise DomainError("Нет полей для изменения")
        if any(v is None for v in updates.values()):
            raise DomainError("Передайте новое значение вместо null")
        with self.db.transaction() as cx:
            row = cx.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if row is None:
                logger.warning("Событие не найдено при обновлении",
                               extra={"event_id": event_id, "action": "update_event", "error": "not_found"})
                raise NotFoundError(f"Запись #{event_id} не найдена")
            existing = self._row(row)
            if patch.expected_version is not None and patch.expected_version != existing.version:
                logger.warning("Конфликт версии при обновлении",
                               extra={"event_id": event_id, "action": "update_event",
                                      "expected_version": patch.expected_version,
                                      "actual_version": existing.version, "error": "conflict"})
                raise ConflictError("Запись изменена другим запросом. Обновите данные и повторите")
            raw = {
                "kind": existing.kind, "title": existing.title,
                "scheduled_date": existing.scheduled_date,
                "scheduled_time": existing.scheduled_time,
                "duration_minutes": existing.duration_minutes,
                "timezone": existing.timezone,
            }
            for key, value in updates.items():
                if key in raw:
                    raw[key] = value
            # При изменении даты, времени или часового пояса событие должно
            # уведомить повторно в новое время.
            notify_reset = any(k in updates for k in ("scheduled_date", "scheduled_time", "timezone"))
            try:
                checked = EventCreate.model_validate(raw)
            except Exception as exc:
                raise DomainError(f"Некорректные параметры события: {exc}") from exc
            if checked.scheduled_date and checked.scheduled_time:
                self._utc(checked.scheduled_date, checked.scheduled_time, checked.timezone)
            duration = checked.duration_minutes or 60
            new_status = updates.get("status", existing.status)
            if existing.kind == Kind.meeting and new_status == "open" and (
                any(k in updates for k in ("scheduled_date", "scheduled_time", "duration_minutes", "status"))
            ):
                conflicts = self._conflicts(cx, day=checked.scheduled_date, moment=checked.scheduled_time,
                                            duration=duration, zone=checked.timezone, exclude=event_id)
                if conflicts:
                    logger.warning("Конфликт времени при обновлении встречи",
                                   extra={"event_id": event_id, "action": "update_event",
                                          "conflict_event_id": conflicts[0].id, "error": "conflict"})
                    raise ConflictError(f"Время пересекается с другой встречей #{conflicts[0].id}: {conflicts[0].title}")
            values = {
                k: v.isoformat() if isinstance(v, (date, time)) else v.value if hasattr(v, "value") else v
                for k, v in updates.items()
            }
            if "scheduled_time" in values:
                values["scheduled_time"] = updates["scheduled_time"].strftime("%H:%M")
            values["updated_at"] = datetime.now(timezone.utc).isoformat()
            if notify_reset:
                values["notified_at"] = None
            setters = ", ".join(f"{key}=?" for key in values)
            cx.execute(f"UPDATE events SET {setters},version=version+1 WHERE id=?", [*values.values(), event_id])
            result = cx.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        event = self._row(result)
        logger.info(
            "Событие обновлено",
            extra={"event_id": event_id, "action": "update_event",
                   "fields": list(updates.keys()), "new_status": event.status,
                   "new_version": event.version},
        )
        return event

    def cancel(self, event_id: int, *, expected_version: int | None = None) -> Event:
        logger.info("Отмена события", extra={"event_id": event_id, "action": "cancel_event",
                                              "expected_version": expected_version})
        return self.update(event_id, EventPatch(status="cancelled", expected_version=expected_version))

    def search(self, query: str, *, kind: Kind | None = None, limit: int = 20,
               include_cancelled: bool = False) -> tuple[list[Event], int]:
        """Offline, deterministic Russian-friendly lexical/fuzzy search; NOT semantic embeddings."""
        from .search import rank
        q = query.strip()
        if len(q) < 2:
            logger.warning("Поиск отклонён: слишком короткий запрос",
                           extra={"action": "search", "query": q, "error": "validation"})
            raise DomainError("Для поиска нужно хотя бы 2 символа")
        filters = []
        args: list[object] = []
        if kind:
            filters.append("kind=?")
            args.append(kind.value)
        if not include_cancelled:
            filters.append("status != 'cancelled'")
        where = "WHERE " + " AND ".join(filters) if filters else ""
        with self.db.connect() as cx:
            rows = cx.execute(f"SELECT * FROM events {where}", args).fetchall()
        scored = [(rank(q, row["title"]), row) for row in rows]
        selected = [(score, row) for score, row in scored if score >= 0.34]
        selected.sort(key=lambda pair: (-pair[0], pair[1]["scheduled_date"] or "9999", pair[1]["id"]))
        found = [self._row(row) for _, row in selected[:limit]]
        logger.info(
            "Поиск событий",
            extra={"action": "search", "query": q, "kind": kind.value if kind else None,
                   "limit": limit, "results": len(found), "total_matches": len(selected)},
        )
        return found, len(selected)

    def import_csv(self, path: Path) -> int:
        """Idempotent CSV import. CSV has no kind: imported records are conservatively tasks."""
        if not path.exists():
            return 0
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not {"id", "date", "time", "text"}.issubset(set(reader.fieldnames or [])):
                raise DomainError("CSV должен содержать id,date,time,text")
            parsed = []
            seen = set()
            for index, row in enumerate(reader, start=2):
                try:
                    external_id = row["id"].strip()
                    if not external_id or external_id in seen:
                        raise ValueError("Empty or duplicate id")
                    seen.add(external_id)
                    day = date.fromisoformat(row["date"].strip())
                    clock = time.fromisoformat(row["time"].strip())
                    title = " ".join(row["text"].split())
                    if not title or len(title) > 500 or clock.second or clock.microsecond:
                        raise ValueError("Invalid title or time")
                    self._utc(day, clock, self.default_timezone)
                except (KeyError, ValueError, DomainError) as exc:
                    raise DomainError(f"Ошибка CSV в строке {index}: {exc}") from exc
                parsed.append((external_id, day.isoformat(), clock.strftime("%H:%M"), title))
        now = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as cx:
            inserted = 0
            for external_id, day, clock, title in parsed:
                cursor = cx.execute("""INSERT OR IGNORE INTO events
                    (kind,title,scheduled_date,scheduled_time,duration_minutes,timezone,status,created_at,updated_at,source,external_id)
                    VALUES('task',?,?,?,NULL,?,'open',?,?,'csv',?)""",
                    (title, day, clock, self.default_timezone, now, now, external_id))
                inserted += cursor.rowcount
        logger.info("Импорт CSV завершён",
                    extra={"action": "import_csv", "path": str(path), "rows_inserted": inserted,
                           "rows_total": len(parsed)})
        return inserted

    def get_session(self, session_id: str) -> dict:
        with self.db.connect() as cx:
            row = cx.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            logger.debug("Сессия не найдена, создано состояние по умолчанию",
                         extra={"session_id": session_id, "action": "get_session"})
            return {"id": session_id, "active_event_id": None, "pending_action": None, "pending_data": None}
        result = dict(row)
        result["pending_data"] = json.loads(result["pending_data"]) if result["pending_data"] else None
        logger.debug("Сессия загружена",
                     extra={"session_id": session_id, "action": "get_session",
                            "pending_action": result.get("pending_action")})
        return result

    def set_session(self, session_id: str, *, active_event_id: int | None = None,
                    pending_action: str | None = None, pending_data: dict | None = None):
        now = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as cx:
            cx.execute("""INSERT INTO sessions(id, active_event_id, pending_action, pending_data, updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                active_event_id=excluded.active_event_id,pending_action=excluded.pending_action,
                pending_data=excluded.pending_data,updated_at=excluded.updated_at""",
                (session_id, active_event_id, pending_action, json.dumps(pending_data, ensure_ascii=False) if pending_data else None, now))
        logger.debug("Сессия обновлена",
                     extra={"session_id": session_id, "action": "set_session",
                            "active_event_id": active_event_id, "pending_action": pending_action})

    def append_dialogue(self, session_id: str, user_text: str, answer: str):
        now = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as cx:
            cx.execute("INSERT OR IGNORE INTO sessions(id,updated_at) VALUES(?,?)", (session_id, now))
            cx.executemany("INSERT INTO messages(session_id,role,content,created_at) VALUES(?,?,?,?)",
                           [(session_id, "user", user_text, now), (session_id, "assistant", answer, now)])
        logger.debug("Диалог записан",
                     extra={"session_id": session_id, "action": "append_dialogue",
                            "user_len": len(user_text), "answer_len": len(answer)})

    def history(self, session_id: str, limit: int = 50) -> list[dict]:
        with self.db.connect() as cx:
            rows = cx.execute("SELECT role,content,created_at FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                              (session_id, limit)).fetchall()
        return [dict(x) for x in reversed(rows)]
