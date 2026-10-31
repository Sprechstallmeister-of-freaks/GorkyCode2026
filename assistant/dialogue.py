"""Explicit limited deterministic dialogue fallback until the LLM adapter is wired.

It is intentionally honest about unsupported free-form language rather than
pretending to provide universal natural-language understanding.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .dates import parse_datetime, period
from .models import ChatOutput, EventCreate, EventPatch, Kind, Status
from .service import Planner, DomainError, ConflictError, NotFoundError


def pretty(event) -> str:
    scheduled = event.scheduled_date.strftime("%d.%m.%Y") if event.scheduled_date else "без срока"
    if event.scheduled_time:
        scheduled += " в " + event.scheduled_time.strftime("%H:%M")
    return f"#{event.id} · {event.title} · {scheduled}"


def clean_title(text: str, consumed: tuple[str, ...], kind: Kind) -> str:
    s = text.strip().replace("ё", "е")
    for phrase in sorted(consumed, key=len, reverse=True):
        s = re.sub(re.escape(phrase), " ", s, flags=re.I)
    # 'в' preceding HH:MM is deliberately removed after the time portion.
    s = re.sub(r"\bв\s+(?=\s|$)", " ", s, flags=re.I)
    if kind == Kind.meeting:
        s = re.sub(r"^(?:добавь|создай|поставь|запланируй|назначь)\s+", "", s, flags=re.I)
        s = re.sub(r"\b(?:у меня|будет|запланирована|нужна)\b", " ", s, flags=re.I)
        s = re.sub(r"\b(?:встреча|встречу)\b", " ", s, flags=re.I)
        s = re.sub(r"\s+", " ", s).strip(" .,!:—-")
        if s.startswith("с ") or s.startswith("со "):
            return "Встреча " + s
        return "Встреча: " + s if s else "Встреча"
    s = re.sub(r"^(?:поставь|создай|добавь)\s+(?:мне\s+)?(?:задачу|напоминание)\s*", "", s, flags=re.I)
    s = re.sub(r"^(?:напомни\s+(?:мне\s+)?(?:о том,?\s+что\s+)?)", "", s, flags=re.I)
    s = re.sub(r"\b(?:нужно|надо)\b", " ", s, flags=re.I)
    s = re.sub(r"\bнапоминание\b", " ", s, flags=re.I)
    s = re.sub(r"\b(?:поставь|создай|добавь)\b", " ", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip(" .,!:—-")


class RuleDialogue:
    def __init__(self, planner: Planner, *, clock=None):
        self.planner = planner
        self.clock = clock or (lambda: datetime.now(ZoneInfo(planner.default_timezone)))

    def _nearest_future_day(self, clock_time: time) -> date:
        """День для события, у которого есть время, но нет даты.

        Ближайшее такое время в будущем: сегодня, если оно ещё не прошло,
        иначе завтра. Спасает от цикла уточнений «напомни на 10 утра».
        """
        now = self.clock()
        candidate = datetime.combine(now.date(), clock_time, tzinfo=now.tzinfo)
        if candidate > now:
            return now.date()
        return now.date() + timedelta(days=1)

    def process(self, message: str, session_id: str, *, record: bool = True) -> ChatOutput:
        raw = message.strip()
        s = raw.lower().replace("ё", "е")
        state = self.planner.get_session(session_id)
        result: ChatOutput
        try:
            if state["pending_action"] == "create_event":
                result = self._finish_pending(raw, s, session_id, state)
            else:
                result = self._route(raw, s, session_id, state)
        except (DomainError, ConflictError, NotFoundError, ValueError) as exc:
            result = ChatOutput(answer=str(exc), session_id=session_id, action="error")
        if record:
            self.planner.append_dialogue(session_id, raw, result.answer)
        return result

    def _finish_pending(self, raw, s, sid, state) -> ChatOutput:
        if s in ("отмена", "отмени", "не надо", "стоп"):
            self.planner.set_session(sid, active_event_id=state["active_event_id"])
            return ChatOutput(answer="Создание отменено.", session_id=sid, action="cancel_pending")
        data = dict(state["pending_data"])
        parsed = parse_datetime(raw, tz=self.planner.default_timezone, now=self.clock())
        clock = parsed.clock
        if clock is None:
            # Короткий ответ «10» / «в 10» на уточнение — это час, а не дата.
            bare = re.fullmatch(r"(?:в\s+)?(\d{1,2})(?:\s+(?:час|часа|часов))?", s)
            if bare and 0 <= int(bare.group(1)) < 24:
                clock = time(int(bare.group(1)), 0)
        if parsed.day:
            data["scheduled_date"] = parsed.day.isoformat()
        if clock:
            data["scheduled_time"] = clock.strftime("%H:%M")
        if not data.get("scheduled_date") and data.get("scheduled_time") and clock and not parsed.day:
            # В ответ на вопрос о дате пользователь снова назвал только время.
            # Не спрашиваем по кругу: ставим на ближайшее такое время в будущем.
            data["scheduled_date"] = self._nearest_future_day(clock).isoformat()
        if not data.get("scheduled_date"):
            self.planner.set_session(sid, active_event_id=state["active_event_id"], pending_action="create_event", pending_data=data)
            title = data.get("title") or "событие"
            answer = f"На какой день поставить «{title}»? Скажи «сегодня», «завтра» или укажи дату."
            if data.get("scheduled_time"):
                answer += f" Время {data['scheduled_time']} уже сохранено."
            return ChatOutput(answer=answer, session_id=sid, action="clarify")
        if not data.get("scheduled_time"):
            self.planner.set_session(sid, active_event_id=state["active_event_id"], pending_action="create_event", pending_data=data)
            return ChatOutput(answer="На какое время? Например, «в 15:00».", session_id=sid, action="clarify")
        event = self.planner.create(EventCreate.model_validate(data))
        self.planner.set_session(sid, active_event_id=event.id)
        return ChatOutput(answer="Создано: " + pretty(event), session_id=sid, action="create", event=event)

    def _route(self, raw, s, sid, state) -> ChatOutput:
        if re.search(r"\b(?:найди|найти|поищи|ищи|когда.*был[а]?|где.*задача)\b", s):
            q = re.sub(r"\b(?:найди|найти|поищи|ищи|старую|старый|задачу|задача|про|по|встреча|встречу|встречи|когда|у меня|была|был|я|когда-то|ставил|ставила|ее|её)\b", " ", s)
            q = re.sub(r"\s+", " ", q).strip(" .?!")
            if not q:
                return ChatOutput(answer="Что именно найти? Например, «найди задачу про договор».", session_id=sid, action="clarify")
            items, total = self.planner.search(q, limit=8)
            if len(items) == 1:
                self.planner.set_session(sid, active_event_id=items[0].id)
            else:
                self.planner.set_session(sid)
            answer = "Ничего похожего не найдено." if not items else "Найдено:\n" + "\n".join(pretty(e) for e in items)
            if total > len(items):
                answer += f"\nИ ещё {total - len(items)} совпадений."
            return ChatOutput(answer=answer, session_id=sid, action="search", events=items)

        if re.search(r"\b(?:что у меня|какие планы|что запланировано|покажи мои|какие задачи|какие встречи|есть ли.*встречи)\b", s):
            rng = period(s, tz=self.planner.default_timezone, now=self.clock())
            if rng is None:
                return ChatOutput(answer="За какой период показать записи: сегодня, завтра, на этой или следующей неделе?", session_id=sid, action="clarify")
            kind = Kind.meeting if "встреч" in s else Kind.task if "задач" in s else None
            items, total = self.planner.list(from_date=rng[0], to_date=rng[1], kind=kind, limit=30)
            answer = "На этот период записей нет." if not items else "Планы:\n" + "\n".join(pretty(e) for e in items)
            if total > len(items):
                answer += f"\nПоказаны первые {len(items)} из {total}."
            return ChatOutput(answer=answer, session_id=sid, action="list", events=items)

        m = re.search(r"(?:#|№)\s*(\d+)", s)
        target = int(m.group(1)) if m else state.get("active_event_id")
        if re.search(r"\b(?:отметь|выполнена|выполнено|готово|завершил|завершена)\b", s):
            if not target:
                return ChatOutput(answer="Укажи номер задачи: «отметь выполненной #42».", session_id=sid, action="clarify")
            event = self.planner.update(target, EventPatch(status=Status.done))
            self.planner.set_session(sid, active_event_id=event.id)
            return ChatOutput(answer="Выполнено: " + pretty(event), session_id=sid, action="update", event=event)
        if re.search(r"\b(?:перенеси|перенести|передвинь|сдвинь)\b", s):
            if not target:
                return ChatOutput(answer="Какое событие перенести? Сначала найди его или назови ID.", session_id=sid, action="clarify")
            parsed = parse_datetime(raw, tz=self.planner.default_timezone, now=self.clock())
            if not parsed.day and not parsed.clock:
                return ChatOutput(answer="На какую дату и время перенести?", session_id=sid, action="clarify")
            patch = EventPatch(scheduled_date=parsed.day, scheduled_time=parsed.clock)
            # Exclude absent values: do not clear a time when only a new date is specified.
            values = {k: v for k, v in patch.model_dump().items() if v is not None}
            event = self.planner.update(target, EventPatch.model_validate(values))
            self.planner.set_session(sid, active_event_id=event.id)
            return ChatOutput(answer="Перенесено: " + pretty(event), session_id=sid, action="update", event=event)

        meeting = bool(re.search(r"\b(?:встреча|встречу|встречи)\b", s))
        reminder = bool(re.search(r"\b(?:напомни|напоминание)\b", s))
        task = bool(re.search(r"\b(?:задачу|задача|нужно|надо)\b", s))
        explicit_create = bool(re.search(
            r"\b(?:добавь|создай|поставь|запланируй|запиши|напомни)\b", s
        ))
        looks_like_question = raw.rstrip().endswith("?") or bool(re.search(
            r"\b(?:что|сколько|какие|есть\s+ли)\b", s
        ))
        if (meeting or reminder or task) and looks_like_question and not explicit_create:
            # A question about existing plans is not an instruction to create a
            # task merely because it contains «нужно», «надо» or «встреча».
            return ChatOutput(
                answer="Похоже, это вопрос о расписании, а не создание записи.",
                session_id=sid, action="unrecognized", needs_llm=True,
            )
        if meeting or reminder or task:
            kind = Kind.meeting if meeting else Kind.reminder if reminder else Kind.task
            parsed = parse_datetime(raw, tz=self.planner.default_timezone, now=self.clock())
            title = clean_title(raw, parsed.consumed, kind)
            if not title and kind == Kind.reminder:
                # «Напоминание на 10 утра» без текста — допустимая запись.
                title = "Напоминание"
            if not title:
                return ChatOutput(answer="Что записать? Укажи название задачи или встречи.", session_id=sid, action="clarify")
            data = {"kind": kind.value, "title": title,
                    "scheduled_date": parsed.day.isoformat() if parsed.day else None,
                    "scheduled_time": parsed.clock.strftime("%H:%M") if parsed.clock else None}
            if kind in (Kind.meeting, Kind.reminder) and not parsed.day and parsed.clock:
                # Время указано, а дата нет: не заставляем пользователя уточнять —
                # выбираем ближайшее такое время (сегодня или завтра).
                data["scheduled_date"] = self._nearest_future_day(parsed.clock).isoformat()
            if kind in (Kind.meeting, Kind.reminder) and (not data["scheduled_date"] or not data["scheduled_time"]):
                self.planner.set_session(sid, active_event_id=state.get("active_event_id"), pending_action="create_event", pending_data=data)
                missing = "дату" if not parsed.day else "время"
                return ChatOutput(answer=f"Уточни {missing} для «{title}».", session_id=sid, action="clarify")
            event = self.planner.create(EventCreate.model_validate(data))
            self.planner.set_session(sid, active_event_id=event.id)
            return ChatOutput(answer="Создано: " + pretty(event), session_id=sid, action="create", event=event)

        return ChatOutput(answer="Пока я понимаю только базовые формулировки: создать задачу/встречу, показать планы, найти или перенести запись. Для свободного диалога подключим LLM.",
                          session_id=sid, action="unrecognized", needs_llm=True)
