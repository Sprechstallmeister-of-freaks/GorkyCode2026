"""High-confidence, read-only natural-language schedule questions.

These queries are handled before the legacy creation rules so conversational
phrases containing «нужно» can never accidentally create a task. More general
language still goes through the regular rule/LLM pipeline.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from .dates import period
from .models import Kind


@dataclass(frozen=True)
class AgendaRequest:
    from_date: date
    to_date: date
    kind: Kind | None = None
    day_label: str | None = None


@dataclass(frozen=True)
class AfterEventRequest:
    query: str


ScheduleRequest = AgendaRequest | AfterEventRequest


class ScheduleQueryParser:
    """Recognise only unambiguous schedule reads, never mutations."""

    _MUTATION = re.compile(
        r"\b(?:создай|добавь|поставь|запланируй|запиши|напомни|"
        r"перенеси|передвинь|сдвинь|отмени|удали|отметь|заверши)\b"
    )
    _AGENDA_MARKERS = (
        re.compile(r"\bчто\s+(?:мне\s+)?(?:нужно\s+)?(?:будет\s+)?(?:сделать|делать)\b"),
        re.compile(r"\bчто\s+(?:у\s+меня|запланировано|предстоит)\b"),
        re.compile(r"\bкакие\s+(?:планы|дела|задачи|встречи|события)\b"),
        re.compile(r"\bсколько\s+(?:у\s+меня\s+)?(?:дел|задач|событий|встреч|созвонов)\b"),
        re.compile(r"\bесть\s+(?:ли\s+)?(?:что|какие|планы|дела|задачи|встречи|события)\b"),
        re.compile(r"\bпокажи\s+(?:мои\s+)?(?:планы|дела|задачи|встречи|события)\b"),
    )
    _AFTER = re.compile(
        r"^(?P<query>.+?)\s*[,.;—–-]?\s*"
        r"после\s+(?:нее|него|этого|этой\s+(?:записи|задачи|встречи|события))\b"
        r"(?P<tail>.*)$"
    )
    _AFTER_QUESTION = re.compile(
        r"\b(?:есть|что|какие|планы|дела|задачи|встречи|события|запланировано)\w*\b"
    )

    def parse(self, text: str, *, now: datetime, timezone: str) -> ScheduleRequest | None:
        raw = " ".join(text.strip().split())
        normalized = raw.lower().replace("ё", "е")
        if not raw or self._MUTATION.search(normalized):
            return None

        after = self._parse_after(normalized)
        if after is not None:
            return after

        if not any(marker.search(normalized) for marker in self._AGENDA_MARKERS):
            return None
        date_range = period(normalized, tz=timezone, now=now)
        if date_range is None:
            return None

        kind = None
        if re.search(r"\bвстреч\w*\b", normalized):
            kind = Kind.meeting
        elif re.search(r"\bзадач\w*\b", normalized):
            kind = Kind.task
        elif re.search(r"\bнапоминан\w*\b", normalized):
            kind = Kind.reminder

        label = None
        for expression in ("послезавтра", "завтра", "сегодня"):
            if re.search(rf"\b{expression}\b", normalized):
                label = expression
                break
        return AgendaRequest(*date_range, kind=kind, day_label=label)

    def _parse_after(self, text: str) -> AfterEventRequest | None:
        match = self._AFTER.match(text)
        if not match or not self._AFTER_QUESTION.search(match.group("tail")):
            return None
        query = match.group("query").strip(" \t,.!?;:—–-«»\"'")
        query = re.sub(r"^(?:а|и)\s+", "", query)
        if not 2 <= len(query) <= 500:
            return None
        return AfterEventRequest(query=query)
