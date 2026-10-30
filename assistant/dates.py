"""Deterministic partial Russian date/time parser for demo mode.

NOT a replacement for a language model: unsupported utterances are surfaced as such.
All relative expressions use the configured IANA timezone, not the server's local clock.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS = {"понедельник": 0, "понедельника": 0, "вторник": 1, "вторника": 1,
            "среду": 2, "среда": 2, "среды": 2, "четверг": 3, "четверга": 3,
            "пятницу": 4, "пятница": 4, "пятницы": 4, "субботу": 5, "суббота": 5,
            "воскресенье": 6, "воскресенья": 6}
NUMBER_WORDS = {"час": 1, "часа": 1, "часов": 1, "два": 2, "две": 2, "три": 3,
                "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10}


@dataclass(frozen=True)
class DateTimeParse:
    day: date | None
    clock: time | None
    consumed: tuple[str, ...]


def now_local(tz: str, now: datetime | None = None) -> datetime:
    zone = ZoneInfo(tz)
    return (now or datetime.now(zone)).astimezone(zone)


def parse_datetime(text: str, *, tz: str, now: datetime | None = None) -> DateTimeParse:
    s = text.lower().replace("ё", "е")
    current = now_local(tz, now)
    day = None
    clock = None
    consumed = []

    match = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", s)
    if match:
        try:
            day = date.fromisoformat(match.group())
            consumed.append(match.group())
        except ValueError:
            pass
    if day is None:
        for expression, delta in (("послезавтра", 2), ("завтра", 1), ("сегодня", 0)):
            if re.search(rf"\b{expression}\b", s):
                day = current.date() + timedelta(days=delta)
                consumed.append(expression)
                break
    if day is None:
        m = re.search(r"\bчерез\s+(\d+|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять)\s+(час|часа|часов|минуту|минуты|минут|дня|дней|день)\b", s)
        if m:
            count = int(m.group(1)) if m.group(1).isdigit() else NUMBER_WORDS[m.group(1)]
            unit = m.group(2)
            delta = timedelta(hours=count) if unit.startswith("час") else timedelta(minutes=count) if unit.startswith("минут") else timedelta(days=count)
            from datetime import timezone
            shifted = (current.astimezone(timezone.utc) + delta).astimezone(ZoneInfo(tz))
            day = shifted.date()
            clock = shifted.time().replace(second=0, microsecond=0)
            consumed.append(m.group())
    if day is None and re.search(r"\bдо конца недели\b", s):
        day = current.date() + timedelta(days=6 - current.weekday())
        consumed.append("до конца недели")
    if day is None:
        m = re.search(r"\b(?:в|во|на)\s+(понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)\b", s)
        if m:
            target = WEEKDAYS[m.group(1)]
            days = (target - current.weekday()) % 7
            day = current.date() + timedelta(days=days)
            consumed.append(m.group())
    if day is None:
        m = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](20\d{2}))?\b", s)
        if m:
            year = int(m.group(3)) if m.group(3) else current.year
            try:
                day = date(year, int(m.group(2)), int(m.group(1)))
                if not m.group(3) and day < current.date():
                    day = date(year + 1, int(m.group(2)), int(m.group(1)))
                consumed.append(m.group())
            except ValueError:
                pass

    if clock is None:
        m = re.search(r"\b(?:в\s+)?([01]?\d|2[0-3]):([0-5]\d)\b", s)
        if m:
            clock = time(int(m.group(1)), int(m.group(2)))
            consumed.append(m.group())
        else:
            m = re.search(r"\bв\s+(\d{1,2})(?:\s+(утра|дня|вечера|ночи))\b", s)
            if m:
                hour = int(m.group(1))
                part = m.group(2)
                if hour <= 12:
                    if part in ("дня", "вечера") and hour < 12:
                        hour += 12
                    if part in ("ночи", "утра") and hour == 12:
                        hour = 0
                if 0 <= hour < 24:
                    clock = time(hour, 0)
                    consumed.append(m.group())
            if clock is None:
                m = re.search(r"\bв\s+(\d{1,2})\b(?!\s*[./])", s)
                if m and 0 <= int(m.group(1)) < 24:
                    clock = time(int(m.group(1)), 0)
                    consumed.append(m.group())
    return DateTimeParse(day=day, clock=clock, consumed=tuple(consumed))


def period(text: str, *, tz: str, now: datetime | None = None) -> tuple[date, date] | None:
    s = text.lower().replace("ё", "е")
    current = now_local(tz, now).date()
    if "на следующей неделе" in s or "следующую неделю" in s:
        first = current + timedelta(days=7 - current.weekday())
        return first, first + timedelta(days=6)
    if "на этой неделе" in s or "эту неделю" in s or "на неделю" in s:
        first = current - timedelta(days=current.weekday())
        return first, first + timedelta(days=6)
    parsed = parse_datetime(text, tz=tz, now=now)
    if parsed.day is not None:
        return parsed.day, parsed.day
    return None
