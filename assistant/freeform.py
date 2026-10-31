"""Извлечение сущностей из свободной формулировки на русском.

Мощный локальный парсер естественного языка для календаря задач/встреч.
Работает без LLM и обрабатывает сложные, разговорные и неточные фразы вида:

    "Если я не приду на очко не брито то что нужно будет еще сделать?"
    "Созвон с Орбитой по презентации после нее есть что-то в тот день?"
    "Завтра в 15:00 встреча с Иваном"
    "Напомни про договор в пятницу в 10:00"
    "Какие события у меня на этой неделе?"

Не зависит от LLM — работает локально regex-правилами и нечетким сопоставлением слов.
Используется в офлайн-режиме (когда LLM-провайдер не подключён)
и проходит через фильтр недопустимого контента.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .dates import period
from .models import Kind

# Слова и фразы, при наличии которых парсер отказывается создавать событие.
BLOCKED_PATTERNS = [
    r"\b(убил|убий|убей|зареж|реж|резак|крови|покрою|покроить)\w*\b",
    r"\b(еврей|евреи|евреев|евреям|жид|жидов|негр|негры|черножоп|пидор|педераст)\w*\b",
    r"\b(насилие|изнасилова|избив|бей|бейся|бить|битьё)\w*\b",
    r"\b(террорист|взорвать|взорв|бомбу)\w*\b",
    r"\b(сука|бля|блядь|хуй|пизда|ебать|ебал|ебануть|на хуй|по хуй|с ума сошел|с ума сошла)\w*\b",
]


def is_blocked_content(text: str) -> bool:
    """Возвращает True, если в тексте есть недопустимый контент."""
    s = text.lower().replace("ё", "е")
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, s):
            return True
    return False


def _word_similarity(a: str, b: str) -> bool:
    """Нечеткое сравнение двух слов: True, если они похожи (допускаются опечатки).

    Считается совпадением, если:
    - слова идентичны;
    - одно слово входит в другое как подстрока и длина разницы не более 3 символов;
    - расстояние Левенштейна <= 2 для коротких слов (<=6), <= 3 для длинных.
    """
    a = a.strip()
    b = b.strip()
    if not a or not b:
        return False
    if a == b:
        return True
    # подстрока
    if len(a) >= 3 and a in b:
        return True
    if len(b) >= 3 and b in a:
        return True
    # расстояние Левенштейна
    max_dist = 2 if max(len(a), len(b)) <= 6 else 3
    return _levenshtein(a, b) <= max_dist


def _levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            current[j] = min(
                current[j - 1] + 1,      # вставка
                previous[j] + 1,         # удаление
                previous[j - 1] + cost,  # замена
            )
        previous = current
    return previous[-1]


def _contains_fuzzy(text_words: list[str], patterns: list[str]) -> bool:
    """Возвращает True, если среди слов текста есть слово, похожее на любой из паттернов."""
    for word in text_words:
        for pat in patterns:
            if _word_similarity(word, pat):
                return True
    return False


@dataclass
class ExtractedEvent:
    title: str | None = None
    kind: Kind | None = None
    start_date: date | None = None
    end_date: date | None = None
    start_time: time | None = None
    duration_minutes: int | None = None
    recurrence: str | None = None  # daily | weekday | weekly
    confidence: float = 0.0
    missing: list[str] = field(default_factory=list)
    raw: str = ""
    is_query: bool = False  # True — вопрос о существующих событиях, а не создание
    is_conditional: bool = False  # True — условная конструкция «если…», «когда…»

    def args_dict(self) -> dict:
        """Аргументы для create_event без полей, которые ещё не известны."""
        args: dict = {}
        if self.title:
            args["title"] = self.title
        if self.kind:
            args["kind"] = self.kind.value
        if self.start_date:
            args["scheduled_date"] = self.start_date.isoformat()
        if self.start_time:
            args["scheduled_time"] = self.start_time.strftime("%H:%M")
        if self.duration_minutes:
            args["duration_minutes"] = self.duration_minutes
        return args


class FreeFormParser:
    """Извлекает дату, время, название, тип и повтор из свободного текста."""

    def __init__(self, default_timezone: str = "Europe/Moscow"):
        self.default_timezone = default_timezone

    # ------------------------------------------------------------------ #
    # Основная точка входа
    # ------------------------------------------------------------------ #
    def parse(self, text: str, now: datetime | None = None) -> ExtractedEvent:
        now = now or datetime.now(ZoneInfo(self.default_timezone))
        raw = text.strip()
        s = raw.lower().replace("ё", "е")
        words = re.findall(r"\w+", s)
        e = ExtractedEvent(raw=raw)

        # Блокируем недопустимый контент.
        if is_blocked_content(raw):
            return e  # confidence=0.0, title=None -> будет обработан как нераспознанное

        # --- 1. Условная конструкция «если…, когда…» ---
        e.is_conditional = bool(
            re.search(r"\b(если|когда|в случае|случае если|допустим)\b", s)
        )

        # --- 2. Определяем, это вопрос о существующих событиях или создание ---
        e.is_query = self._is_query(s)
        if e.is_query:
            self._extract_query_params(e, s, now)
            e.confidence = 0.7 if e.start_date else 0.4
            e.missing = []
            return e

        # --- 3. Тип события (с нечетким совпадением) ---
        e.kind = self._detect_kind_fuzzy(words, s)

        # --- 4. Повторение / период ---
        self._extract_recurrence(e, s, now)

        # --- 5. Дата ---
        self._extract_date(e, s, now)

        # --- 6. Время ---
        self._extract_time(e, s)

        # --- 7. Длительность ---
        self._extract_duration(e, s)

        # --- 8. Название (менее агрессивная очистка) ---
        title = self._extract_title(raw, s, e.kind)
        if title:
            e.title = title

        e.confidence = self._confidence(e)
        e.missing = self._missing(e)
        return e

    # ------------------------------------------------------------------ #
    # Вопросы о существующих событиях
    # ------------------------------------------------------------------ #
    @staticmethod
    def _is_query(s: str) -> bool:
        query_markers = (
            "есть что", "что запланиров", "какие события", "какие задачи",
            "какие встречи", "что у меня", "покажи мои", "что на ", "что в ",
            "есть ли", "в тот день", "в этот день", "за этот день", "за тот день",
            "есть сегодня", "есть завтра", "что нужно будет", "что будет",
            "что мне делать", "что сделать", "что запланировано",
            "сколько", "как мне", "как можно", "что требуется",
            "чтобы я мог", "чтобы я могла", "сколько дел",
        )
        return any(m in s for m in query_markers)

    def _extract_query_params(self, e: ExtractedEvent, s: str, now: datetime) -> None:
        # A general agenda includes every kind. In particular, words such as
        # «созвон» inside a title must not filter out CSV records stored as tasks.
        e.kind = None
        if re.search(r"\bвстреч\w*\b", s):
            e.kind = Kind.meeting
        elif re.search(r"\bзадач\w*\b", s):
            e.kind = Kind.task
        elif re.search(r"\bнапоминан\w*\b", s):
            e.kind = Kind.reminder

        if any(w in s for w in ("в тот день", "в этот день")):
            e.start_date = None
            e.end_date = None
            return

        date_range = period(s, tz=self.default_timezone, now=now)
        if date_range is not None:
            # Single-day questions must have a closed range, not an open-ended
            # `from_date` query. Shared date parsing also keeps послезавтра exact.
            e.start_date, e.end_date = date_range
        elif "месяц" in s:
            e.start_date = now.date()
            e.end_date = now.date() + timedelta(days=30)

    # ------------------------------------------------------------------ #
    # Тип события (с нечетким совпадением)
    # ------------------------------------------------------------------ #
    def _detect_kind_fuzzy(self, words: list[str], s: str) -> Kind:
        meeting_patterns = ["встреч", "созвон", "созвонить", "звонок", "звонить",
                            "митинг", "колл", "конференц", "обсуждение", "демо"]
        reminder_patterns = ["напомни", "напоминание", "напомнить", "напомни-moi"]
        if _contains_fuzzy(words, meeting_patterns) or any(w in s for w in meeting_patterns):
            return Kind.meeting
        if _contains_fuzzy(words, reminder_patterns) or any(w in s for w in reminder_patterns):
            return Kind.reminder
        return Kind.task

    # ------------------------------------------------------------------ #
    # Повторение
    # ------------------------------------------------------------------ #
    def _extract_recurrence(self, e: ExtractedEvent, s: str, now: datetime) -> None:
        if any(w in s for w in ("всю неделю", "в эту неделю", "каждый день эту неделю")):
            e.recurrence = "daily"
            e.start_date = now.date()
            e.end_date = now.date() + timedelta(days=(6 - now.date().weekday()))
        elif any(w in s for w in ("каждый день", "ежедневно")):
            e.recurrence = "daily"
        elif any(w in s for w in ("по будням", "в будни", "пн-пт", "по рабочим")):
            e.recurrence = "weekday"
        elif any(w in s for w in ("каждую неделю", "еженедельно")):
            e.recurrence = "weekly"
        elif any(w in s for w in ("каждый месяц", "ежемесячно")):
            e.recurrence = "monthly"

    # ------------------------------------------------------------------ #
    # Дата
    # ------------------------------------------------------------------ #
    def _extract_date(self, e: ExtractedEvent, s: str, now: datetime) -> None:
        # "через N дней/недель"
        m = re.search(r"через\s+(\d+)\s*(день|дня|дней|недел|неделя|недели)", s)
        if m:
            val = int(m.group(1))
            unit = m.group(2)
            days = val * 7 if unit.startswith("недел") else val
            e.start_date = now.date() + timedelta(days=days)
            return

        # "на/в/к сегодня/завтра/послезавтра"
        m = re.search(r"(?:на|в|к)\s+(сегодня|завтра|послезавтра)", s)
        if m:
            shift = {"сегодня": 0, "завтра": 1, "послезавтра": 2}[m.group(1)]
            e.start_date = now.date() + timedelta(days=shift)
            return

        # standalone
        if not e.start_date:
            m = re.search(r"\b(сегодня|завтра|послезавтра)\b", s)
            if m:
                shift = {"сегодня": 0, "завтра": 1, "послезавтра": 2}[m.group(1)]
                e.start_date = now.date() + timedelta(days=shift)
                return

        # дни недели
        if not e.start_date:
            weekdays = {
                "понедельник": 0, "вторник": 1, "среда": 2, "четверг": 3,
                "пятница": 4, "суббота": 5, "воскресенье": 6,
            }
            for wday_name, wday_idx in weekdays.items():
                if wday_name in s:
                    today_idx = now.date().weekday()
                    days_ahead = (wday_idx - today_idx) % 7
                    if days_ahead == 0:
                        days_ahead = 7
                    e.start_date = now.date() + timedelta(days=days_ahead)
                    break

        # ISO дата
        if not e.start_date:
            m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", s)
            if m:
                e.start_date = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

        # точка дата
        if not e.start_date:
            m = re.search(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\b", s)
            if m:
                y = int(m.group(3)) if m.group(3) else now.year
                try:
                    e.start_date = date(y, int(m.group(2)), int(m.group(1)))
                except ValueError:
                    pass

    # ------------------------------------------------------------------ #
    # Время
    # ------------------------------------------------------------------ #
    def _extract_time(self, e: ExtractedEvent, s: str) -> None:
        # HH:MM
        m = re.search(r"\b(\d{1,2}):(\d{2})\b", s)
        if m:
            e.start_time = time(int(m.group(1)), int(m.group(2)))
            return
        # "в 15 часов", "в 3 часа"
        m = re.search(r"\bв\s+(\d{1,2})\s*(час|часа|часов)\b", s)
        if m:
            e.start_time = time(int(m.group(1)), 0)
            return
        # "к 15:00", "к 15"
        m = re.search(r"\bк\s+(\d{1,2})(?::(\d{2}))?\b", s)
        if m:
            hh = int(m.group(1))
            mm = int(m.group(2)) if m.group(2) else 0
            e.start_time = time(hh, mm)

    # ------------------------------------------------------------------ #
    # Длительность
    # ------------------------------------------------------------------ #
    def _extract_duration(self, e: ExtractedEvent, s: str) -> None:
        m = re.search(r"(?:на|длительностью)\s+(\d+)\s*(мин|минута|минуты|минут|час|часа|часов)", s)
        if m:
            val = int(m.group(1))
            unit = m.group(2)
            e.duration_minutes = val * 60 if unit.startswith("час") else val

    # ------------------------------------------------------------------ #
    # Название события (мягкая очистка)
    # ------------------------------------------------------------------ #
    def _extract_title(self, raw: str, s: str, kind: Kind) -> str | None:
        t = raw

        # Убираем только четкие глаголы действия и служебные конструкции в начале
        start_patterns = (
            r"^(?:поставь|добавь|создай|запланируй|назначь|запиши|созвони|созвониться|позвони|напомни)\s+",
            r"^(?:мне\s+)?(?:задачу|встречу|созвон|событие|напоминание)\s+",
            r"^(?:мне\s+)?(?:нужно|надо|следует)\s+",
        )
        for pat in start_patterns:
            t = re.sub(pat, " ", t, flags=re.I)

        # Убираем «в календарь/календарик»
        t = re.sub(r"\bв\s+(?:календар|календарик|план)\w*\b", " ", t, flags=re.I)

        # Убираем даты и время
        t = re.sub(r"\b\d{1,2}:\d{2}\b", " ", t)
        t = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", t)
        t = re.sub(r"\b\d{1,2}\.\d{1,2}\.?\d{0,4}\b", " ", t)
        t = re.sub(r"\bв\s+\d{1,2}\s*(час|часа|часов)\b", " ", t, flags=re.I)
        t = re.sub(r"\bк\s+\d{1,2}(?::\d{2})?\b", " ", t, flags=re.I)

        # Убираем относительные даты и периоды (но не слова в середине названия, если они часть события)
        t = re.sub(
            r"\b(сегодня|завтра|послезавтра|всю неделю|в эту неделю|"
            r"каждый день эту неделю|каждый день|ежедневно|"
            r"по будням|в будни|пн-пт|по рабочим|каждую неделю|еженедельно|"
            r"каждый месяц|ежемесячно|через\s+\d+\s*(день|дня|дней|недел|неделя|недели)|"
            r"понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)\b",
            " ", t, flags=re.I,
        )
        # Убираем местоимения-ссылки
        t = re.sub(r"\b(после нее|после этого|после него|в тот день|в этот день)\b", " ", t, flags=re.I)

        # Убираем остаточные предлоги в начале/конце
        t = re.sub(r"^\s*(с|со|о|об|про|у|к|на|в|за|по|для|от|если|когда)\s+", " ", t, flags=re.I)
        t = re.sub(r"^\s*(?:что\s+)?я\s+(?:(?:хотел|хотела)\s+бы|хочу)\s+",
                   " ", t, flags=re.I)
        t = re.sub(r"\s+(с|со|о|об|про|у|к|на|в|за|по|для|от)\s*$", " ", t, flags=re.I)

        t = re.sub(r"\s+", " ", t).strip(" .,!:—-")

        if not t or len(t) < 2:
            return None
        return t[:120]

    # ------------------------------------------------------------------ #
    # Уверенность и пропущенные поля
    # ------------------------------------------------------------------ #
    def _confidence(self, e: ExtractedEvent) -> float:
        score = 0.0
        if e.title:
            score += 0.4
        if e.start_date:
            score += 0.3
        if e.start_time:
            score += 0.2
        if e.kind:
            score += 0.1
        return round(score, 2)

    def _missing(self, e: ExtractedEvent) -> list[str]:
        missing: list[str] = []
        if e.is_query:
            return missing
        if not e.title:
            missing.append("title")
        if e.kind in (Kind.meeting, Kind.reminder):
            if not e.start_date:
                missing.append("date")
            if not e.start_time:
                missing.append("time")
        return missing
