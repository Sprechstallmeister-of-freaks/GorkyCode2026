"""Rule-first dialogue orchestration with a replaceable LLM provider.

The database remains the source of truth. Model suggestions always pass through
ToolRegistry validation and Planner domain rules. Session state handles follow-ups.
"""
from __future__ import annotations

import re
import logging
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from .contracts import LLMContext, ParsedCommand
from .dates import parse_datetime
from .dialogue import RuleDialogue, pretty
from .freeform import ExtractedEvent, FreeFormParser, is_blocked_content
from .llm_provider import LLMProvider, NullLLMProvider
from .logging_config import get_request_id
from .models import ChatOutput, Event, EventCreate, Kind
from .read_queries import AfterEventRequest, AgendaRequest, ScheduleQueryParser
from .service import NotFoundError, Planner
from .tools import ToolRegistry, DEFINITIONS

logger = logging.getLogger("assistant.routing")

CANCEL_WORDS = {"отмена", "отмени", "стоп", "не надо", "передумал"}
YES_WORDS = {"да", "подтверждаю", "подтвердить", "да, отменяй", "отменяй"}
NO_WORDS = {"нет", "не отменяй", *CANCEL_WORDS}
ORDINALS = {"первую": 0, "первый": 0, "первое": 0, "1": 0,
            "вторую": 1, "второй": 1, "второе": 1, "2": 1,
            "третью": 2, "третий": 2, "третье": 2, "3": 2}
CONTEXT_DATE_REFERENCE = re.compile(
    r"\b(?:на|в)\s+(?:этот|тот)(?:\s+же)?\s+день\b|"
    r"\bна\s+(?:эту|ту)(?:\s+же)?\s+дату\b",
    re.IGNORECASE,
)


class ChatRouter:
    def __init__(self, planner: Planner, *, provider: LLMProvider | None = None,
                 rules: RuleDialogue | None = None):
        self.planner = planner
        self.rules = rules or RuleDialogue(planner)
        self.provider = provider or NullLLMProvider()
        self.tools = ToolRegistry(planner)
        self.freeform = FreeFormParser(planner.default_timezone)
        self.schedule_queries = ScheduleQueryParser()
        self.llm_configured = not isinstance(self.provider, NullLLMProvider)

    @property
    def llm_connected(self) -> bool:
        return bool(getattr(self.provider, "connected", self.llm_configured))

    def process(self, message: str, session_id: str) -> ChatOutput:
        """Exactly one user+assistant history pair per incoming message."""
        raw = message.strip()
        state = self.planner.get_session(session_id)

        # Защита от недопустимого контента (насилие, хейт, ненормативная лексика)
        # блокируется до любой обработки — ни LLM, ни парсер не должны его создавать.
        if is_blocked_content(raw):
            logger.warning("Заблокирован недопустимый контент",
                           extra={"session_id": session_id, "action": "chat_process",
                                  "message_preview": raw[:120], "error": "blocked_content"})
            return ChatOutput(answer="Я не могу создать событие с таким содержанием. Оно противоречит правилам безопасности.",
                              session_id=session_id, action="blocked", needs_llm=False)

        logger.info(
            "Обработка сообщения чата",
            extra={"session_id": session_id, "action": "chat_process",
                   "message_len": len(raw), "message_preview": raw[:120],
                   "pending_action": state.get("pending_action")},
        )
        try:
            effective_raw, referenced_date = self._resolve_date_reference(raw, state)
            if referenced_date is not None:
                # Transient server-side context; it is never supplied as an
                # event_id or trusted LLM argument and is not persisted in JSON.
                state["_referenced_date"] = referenced_date
            if state.get("pending_action") in {
                "select_event", "select_after_event", "confirm_cancel", "complete_command"
            }:
                result = self._continue(raw, session_id, state)
            elif (schedule_request := self.schedule_queries.parse(
                effective_raw, now=self.rules.clock(), timezone=self.planner.default_timezone
            )) is not None:
                # Informational questions must reach read-only logic before the
                # legacy «нужно/надо» task-creation fallback or an LLM tool call.
                if isinstance(schedule_request, AgendaRequest):
                    result = self._answer_agenda(schedule_request, session_id)
                elif isinstance(schedule_request, AfterEventRequest):
                    result = self._answer_after_event(schedule_request, session_id, state)
                else:  # pragma: no cover - exhaustiveness guard for future request types
                    raise TypeError("Неизвестный тип вопроса о расписании")
            else:
                # Existing rule handler retains its own create-event clarification.
                result = self.rules.process(effective_raw, session_id, record=False)
                if result.needs_llm and result.action != "unrecognized":
                    # LLM needed for a recognized-but-complex rule action
                    result = self._interpret(effective_raw, session_id, state)
                elif result.action == "search" and not result.events:
                    result = self._search_with_synonyms(raw, session_id, state)
                elif result.needs_llm and result.action == "unrecognized":
                    # Free-form phrase not covered by rules.
                    if isinstance(self.provider, NullLLMProvider):
                        # Офлайн-режим — локальный парсер
                        parsed = self.freeform.parse(
                            effective_raw, now=datetime.now(ZoneInfo(self.planner.default_timezone))
                        )
                        logger.info(
                            "FreeFormParser результат",
                            extra={"session_id": session_id, "action": "freeform_parse",
                                   "title": parsed.title, "kind": parsed.kind.value if parsed.kind else None,
                                   "start_date": str(parsed.start_date) if parsed.start_date else None,
                                   "start_time": str(parsed.start_time) if parsed.start_time else None,
                                   "recurrence": parsed.recurrence, "confidence": parsed.confidence,
                                   "missing": parsed.missing, "is_query": parsed.is_query,
                                   "is_conditional": parsed.is_conditional},
                        )
                        if parsed.is_query:
                            result = self._query_from_freeform(parsed, session_id, state)
                        elif parsed.title:
                            # Есть название — пробуем создать событие, даже если не хватает даты/времени.
                            # Недостающие поля будем уточнять через complete_command.
                            if parsed.confidence >= 0.3 and not parsed.missing:
                                result = self._create_from_freeform(parsed, session_id, state)
                            else:
                                # Не всё ясно — спрашиваем у пользователя и сохраняем черновик
                                first_missing = parsed.missing[0] if parsed.missing else "date"
                                question = self._missing_question(first_missing)
                                self.planner.set_session(
                                    session_id,
                                    pending_action="complete_command",
                                    pending_data={"tool": "create_event",
                                                  "arguments": parsed.args_dict()},
                                )
                                result = ChatOutput(answer=question, session_id=session_id,
                                                    action="clarify", needs_llm=False)
                        else:
                            result = ChatOutput(
                                answer="Не удалось распознать задачу. Попробуй перефразировать.",
                                session_id=session_id, action="unrecognized", needs_llm=True)
                    else:
                        # LLM подключён — определяем, вопрос ли это, и загружаем события из БД для контекста.
                        probe = self.freeform.parse(
                            effective_raw, now=datetime.now(ZoneInfo(self.planner.default_timezone))
                        )
                        events_ctx = None
                        if probe.is_query:
                            # Загружаем события из БД для контекста модели.
                            from_date = probe.start_date
                            to_date = probe.end_date
                            items, _ = self.planner.list(from_date=from_date, to_date=to_date,
                                                         kind=probe.kind, limit=50)
                            events_ctx = [e.model_dump(mode="json") for e in items]
                        result = self._interpret(effective_raw, session_id, state, events=events_ctx)
                result = self._maybe_describe(raw, result, session_id, state)
        except (ValueError, TypeError, ValidationError) as exc:
            logger.exception("Ошибка валидации при обработке сообщения",
                             extra={"session_id": session_id, "action": "chat_process",
                                    "error_type": type(exc).__name__})
            result = ChatOutput(answer=f"Некорректные параметры: {exc}", session_id=session_id, action="error")
        self.planner.append_dialogue(session_id, raw, result.answer)
        logger.info(
            "Сообщение чата обработано",
            extra={"session_id": session_id, "action": result.action, "mode": result.mode,
                   "needs_llm": result.needs_llm, "has_event": result.event is not None,
                   "events_count": len(result.events), "answer_preview": result.answer[:120]},
        )
        return result

    def _resolve_date_reference(self, text: str, state: dict) -> tuple[str, date | None]:
        """Resolve «на этот день» from the event discussed in the previous turn."""
        if not CONTEXT_DATE_REFERENCE.search(text) or not state.get("active_event_id"):
            return text, None
        try:
            active = self.planner.get(state["active_event_id"])
        except NotFoundError:
            return text, None
        if active.scheduled_date is None:
            return text, None
        resolved = active.scheduled_date
        effective = CONTEXT_DATE_REFERENCE.sub(f"на {resolved.isoformat()}", text, count=1)
        return effective, resolved

    def _context(self, sid: str, state: dict | None = None, events: list[dict] | None = None) -> LLMContext:
        # The model sees ONLY wall-clock time and the configured IANA zone.
        # Session state is deliberately never included in its JSON context.
        zone = self.planner.default_timezone
        return LLMContext(now=datetime.now(ZoneInfo(zone)), timezone=zone, events=events)

    def _interpret(self, text: str, sid: str, state: dict, events: list[dict] | None = None) -> ChatOutput:
        logger.info("Передача текста в LLM-провайдер",
                    extra={"session_id": sid, "action": "llm_interpret",
                           "text_len": len(text), "text_preview": text[:120],
                           "has_events": bool(events), "events_count": len(events) if events else 0})
        start = time.perf_counter()
        try:
            command = ParsedCommand.model_validate(self.provider.interpret(text, self._context(sid, state, events), self.tools.specs()))
        except Exception:
            latency_ms = (time.perf_counter() - start) * 1000
            logger.exception("Ошибка интерпретации LLM",
                             extra={"session_id": sid, "action": "llm_interpret",
                                    "llm_latency_ms": round(latency_ms, 2),
                                    "error_type": "provider_error"})
            return ChatOutput(answer="Не удалось обработать свободную формулировку. Попробуй перефразировать запрос.",
                              session_id=sid, action="unrecognized", needs_llm=True)
        latency_ms = (time.perf_counter() - start) * 1000
        logger.info("LLM вернула команду",
                    extra={"session_id": sid, "action": "llm_interpret",
                           "tool": command.tool, "target_query": command.target_query,
                           "arguments": command.arguments,
                           "llm_latency_ms": round(latency_ms, 2)})
        if command.tool == "unknown":
            return ChatOutput(answer="Не удалось определить действие. Уточни, что нужно создать, найти или изменить.",
                              session_id=sid, action="unrecognized", needs_llm=True)
        result = self._execute(command, sid, state)
        result.mode = "deepseek"
        return result

    def _execute(self, command: ParsedCommand, sid: str, state: dict, *, confirmed: bool = False) -> ChatOutput:
        name = command.tool
        if name == "unknown":
            logger.warning("LLM вернула неизвестный инструмент",
                           extra={"session_id": sid, "action": "tool_execute", "tool": "unknown"})
            return ChatOutput(answer="Не удалось определить действие.", session_id=sid, action="unrecognized")
        args = dict(command.arguments)
        logger.info(
            "Выполнение инструмента",
            extra={"session_id": sid, "action": "tool_execute", "tool": name, "arguments": args,
                   "confirmed": confirmed},
        )
        if name in {"update_event", "cancel_event", "get_event"} and not args.get("event_id"):
            if command.target_query:
                rows, _ = self.planner.search(command.target_query, limit=8)
                if not rows:
                    logger.warning("Событие не найдено по запросу",
                                   extra={"session_id": sid, "action": "tool_execute", "tool": name,
                                          "target_query": command.target_query, "error": "not_found"})
                    return ChatOutput(answer="Не нашёл подходящее событие. Уточни его название.", session_id=sid, action="not_found")
                if len(rows) > 1:
                    self.planner.set_session(sid, active_event_id=state.get("active_event_id"),
                        pending_action="select_event", pending_data={"tool": name, "arguments": args,
                        "candidate_ids": [e.id for e in rows], "candidate_versions": [e.version for e in rows]})
                    logger.info("Найдено несколько событий, требуется уточнение",
                                extra={"session_id": sid, "action": "tool_execute", "tool": name,
                                       "candidate_ids": [e.id for e in rows], "candidates_count": len(rows)})
                    return ChatOutput(answer="Нашёл несколько событий. Укажи номер или ID:\n" + "\n".join(
                        f"{idx}. {pretty(e)}" for idx, e in enumerate(rows, 1)),
                        session_id=sid, action="clarify", events=rows)
                args["event_id"] = rows[0].id
                logger.debug("Событие найдено по запросу",
                             extra={"session_id": sid, "action": "tool_execute", "tool": name,
                                    "event_id": rows[0].id, "target_query": command.target_query})
            elif state.get("active_event_id"):
                args["event_id"] = state["active_event_id"]
                logger.debug("Использован активный идентификатор события",
                             extra={"session_id": sid, "action": "tool_execute", "tool": name,
                                    "event_id": state["active_event_id"]})
            else:
                logger.warning("Не указано событие и нет активного",
                               extra={"session_id": sid, "action": "tool_execute", "tool": name, "error": "clarify"})
                return ChatOutput(answer="Какое событие имеется в виду? Укажи ID или название.", session_id=sid, action="clarify")

        if name == "create_event":
            referenced_date = state.get("_referenced_date")
            if referenced_date is not None:
                # The server, not the model, owns contextual date resolution.
                args["scheduled_date"] = referenced_date.isoformat()
            kind = args.get("kind")
            missing = [field for field in ("title", "scheduled_date", "scheduled_time")
                       if not args.get(field) and (field == "title" or kind in {"meeting", "reminder"})]
            if missing:
                self.planner.set_session(sid, active_event_id=state.get("active_event_id"),
                    pending_action="complete_command", pending_data={"tool": name, "arguments": args})
                logger.info("Требуется уточнение для создания события",
                            extra={"session_id": sid, "action": "tool_execute", "tool": name,
                                   "missing_field": missing[0]})
                return ChatOutput(answer=self._missing_question(missing[0]), session_id=sid, action="clarify")

        if name == "cancel_event" and not confirmed:
            try:
                event = self.planner.get(int(args["event_id"]))
            except (ValueError, TypeError):
                logger.warning("Неверный ID события при отмене",
                               extra={"session_id": sid, "action": "tool_execute", "tool": name,
                                      "event_id": args.get("event_id"), "error": "validation"})
                return ChatOutput(answer="Неверный ID события.", session_id=sid, action="error")
            except Exception as exc:
                from .service import NotFoundError
                if isinstance(exc, NotFoundError):
                    logger.warning("Событие не найдено при отмене",
                                   extra={"session_id": sid, "action": "tool_execute", "tool": name,
                                          "event_id": args.get("event_id"), "error": "not_found"})
                    return ChatOutput(answer=str(exc), session_id=sid, action="not_found")
                raise
            # Pin the existing version so stale confirmations cannot cancel updated records.
            self.planner.set_session(sid, active_event_id=state.get("active_event_id"),
                pending_action="confirm_cancel", pending_data={"tool": name,
                    "arguments": {**args, "expected_version": event.version}})
            logger.info("Требуется подтверждение отмены",
                        extra={"session_id": sid, "action": "tool_execute", "tool": name,
                               "event_id": event.id, "expected_version": event.version})
            return ChatOutput(answer=f"Подтверди отмену «{event.title}» (#{event.id}): да или нет?",
                              session_id=sid, action="clarify")

        # Extra/untrusted keys cannot leak into the tools. Registry handles validation.
        start = time.perf_counter()
        outcome = self.tools.execute(name, args, confirmed_by_user=confirmed)
        tool_latency_ms = (time.perf_counter() - start) * 1000
        if not outcome["ok"]:
            code = outcome.get("error", "error")
            action = "conflict" if code == "conflict" else "not_found" if code == "not_found" else "error"
            logger.warning(
                "Инструмент вернул ошибку",
                extra={"session_id": sid, "action": "tool_execute", "tool": name,
                       "error_code": code, "error_message": outcome.get("message"),
                       "llm_latency_ms": round(tool_latency_ms, 2)},
            )
            return ChatOutput(answer=outcome.get("message", "Ошибка выполнения"), session_id=sid, action=action)

        if "event" in outcome:
            event = Event.model_validate(outcome["event"])
            self.planner.set_session(sid, active_event_id=event.id)
            verb = {"create_event": "Создано", "update_event": "Изменено", "cancel_event": "Отменено", "get_event": "Событие"}[name]
            logger.info(
                "Инструмент выполнен успешно",
                extra={"session_id": sid, "action": "tool_execute", "tool": name,
                       "event_id": event.id, "event_status": event.status,
                       "llm_latency_ms": round(tool_latency_ms, 2)},
            )
            return ChatOutput(answer=f"{verb}: {pretty(event)}", session_id=sid,
                              action=name, event=event)

        events = [Event.model_validate(item) for item in outcome.get("items", [])]
        if name == "search_events" and not events:
            logger.info("Поиск не вернул результатов, пробуем синонимы",
                        extra={"session_id": sid, "action": "tool_execute", "tool": name,
                               "query": args.get("query")})
            return self._search_with_synonyms(args.get("query", ""), sid, state)
        if len(events) == 1 and name == "search_events":
            self.planner.set_session(sid, active_event_id=events[0].id)
        total = outcome.get("total", len(events))
        label = "Найдено" if name == "search_events" else "Планы"
        answer = "Ничего не найдено." if not events else f"{label}:\n" + "\n".join(pretty(e) for e in events)
        if total > len(events):
            answer += f"\nИ ещё {total - len(events)} записей."
        logger.info(
            "Инструмент выполнен успешно",
            extra={"session_id": sid, "action": "tool_execute", "tool": name,
                   "results_count": len(events), "total": total,
                   "llm_latency_ms": round(tool_latency_ms, 2)},
        )
        return ChatOutput(answer=answer, session_id=sid, action=name, events=events)

    @staticmethod
    def _missing_question(field: str) -> str:
        return {"title": "Что записать? Назови событие.",
                "scheduled_date": "На какую дату? Например, «завтра».",
                "scheduled_time": "На какое время? Например, «15:00»."}.get(field, f"Уточни {field}.")

    def _continue(self, raw: str, sid: str, state: dict) -> ChatOutput:
        normalized = raw.lower().replace("ё", "е").strip(" .!?")
        logger.info("Продолжение pending-состояния диалога",
                    extra={"session_id": sid, "action": "dialogue_continue",
                           "pending_action": state.get("pending_action"),
                           "user_input_preview": raw[:120]})
        if normalized in CANCEL_WORDS:
            self.planner.set_session(sid, active_event_id=state.get("active_event_id"))
            logger.info("Действие отменено пользователем",
                        extra={"session_id": sid, "action": "dialogue_continue", "result": "cancelled"})
            return ChatOutput(answer="Действие отменено.", session_id=sid, action="cancel_pending")
        pending = state["pending_action"]
        data = dict(state.get("pending_data") or {})
        if pending == "confirm_cancel":
            if normalized not in YES_WORDS:
                if normalized in NO_WORDS:
                    self.planner.set_session(sid, active_event_id=state.get("active_event_id"))
                    return ChatOutput(answer="Событие не отменено.", session_id=sid, action="cancel_pending")
                return ChatOutput(answer="Ответь «да» или «нет» для подтверждения отмены.", session_id=sid, action="clarify")
            self.planner.set_session(sid, active_event_id=state.get("active_event_id"))
            return self._execute(ParsedCommand.model_validate(data), sid, state, confirmed=True)

        if pending == "select_after_event":
            ids = data["candidate_ids"]
            index = ORDINALS.get(normalized)
            chosen = ids[index] if index is not None and index < len(ids) else None
            match = re.search(r"(?:#|№)\s*(\d+)", raw)
            if match and int(match.group(1)) in ids:
                chosen = int(match.group(1))
            if chosen is None:
                return ChatOutput(answer="Укажи номер варианта (1, 2, 3) или ID с символом #.",
                                  session_id=sid, action="clarify")
            return self._after_event_by_id(chosen, sid)

        if pending == "select_event":
            ids = data["candidate_ids"]
            index = ORDINALS.get(normalized)
            chosen = ids[index] if index is not None and index < len(ids) else None
            match = re.search(r"(?:#|№)\s*(\d+)", raw)
            if match:
                event_id = int(match.group(1))
                if event_id in ids:
                    chosen = event_id
            if chosen is None:
                return ChatOutput(answer="Укажи номер варианта (1, 2, 3) или ID с символом #.", session_id=sid, action="clarify")
            args = dict(data["arguments"])
            args["event_id"] = chosen
            idx = ids.index(chosen)
            if data["tool"] in {"update_event", "cancel_event"}:
                args.setdefault("expected_version", data["candidate_versions"][idx])
            self.planner.set_session(sid, active_event_id=state.get("active_event_id"))
            return self._execute(ParsedCommand(tool=data["tool"], arguments=args), sid, state)

        if pending == "complete_command":
            args = dict(data["arguments"])
            if not args.get("title"):
                args["title"] = raw.strip()
            else:
                parsed = parse_datetime(raw, tz=self.planner.default_timezone,
                                        now=datetime.now(ZoneInfo(self.planner.default_timezone)))
                if parsed.day:
                    args["scheduled_date"] = parsed.day.isoformat()
                if parsed.clock:
                    args["scheduled_time"] = parsed.clock.strftime("%H:%M")
            self.planner.set_session(sid, active_event_id=state.get("active_event_id"))
            return self._execute(ParsedCommand(tool=data["tool"], arguments=args), sid, state)
        logger.warning("Неизвестное pending-состояние диалога",
                       extra={"session_id": sid, "action": "dialogue_continue",
                              "pending_action": pending, "error": "unknown_state"})
        return ChatOutput(answer="Неизвестное состояние диалога.", session_id=sid, action="error")

    @staticmethod
    def _things_word(count: int) -> str:
        if count % 10 == 1 and count % 100 != 11:
            return "дело"
        if count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
            return "дела"
        return "дел"

    def _answer_agenda(self, request: AgendaRequest, sid: str) -> ChatOutput:
        items, total = self.planner.list(
            from_date=request.from_date, to_date=request.to_date,
            kind=request.kind, limit=100,
        )
        single_day = request.from_date == request.to_date
        day = request.from_date.strftime("%d.%m.%Y")
        if single_day and request.day_label:
            period_text = f"На {request.day_label} ({day})"
        elif single_day:
            period_text = f"На {day}"
        else:
            period_text = (f"С {request.from_date.strftime('%d.%m.%Y')} "
                           f"по {request.to_date.strftime('%d.%m.%Y')}")

        if not items:
            answer = f"{period_text} ничего не запланировано"
            answer += " — день свободен." if single_day else "."
        else:
            answer = (f"{period_text} запланировано {total} "
                      f"{self._things_word(total)}:\n" + "\n".join(pretty(event) for event in items))
            if total > len(items):
                answer += f"\nПоказаны первые {len(items)} из {total}."
            elif single_day:
                answer += "\nДругих дел на этот день в календаре нет."
        return ChatOutput(answer=answer, session_id=sid, action="list", events=items)

    def _answer_after_event(self, request: AfterEventRequest, sid: str, state: dict) -> ChatOutput:
        rows, _ = self.planner.resolve_reference(request.query, limit=8)
        if not rows:
            return ChatOutput(answer=f"Не нашёл событие «{request.query}». Уточни его название.",
                              session_id=sid, action="not_found")
        if len(rows) > 1:
            self.planner.set_session(
                sid, active_event_id=state.get("active_event_id"),
                pending_action="select_after_event",
                pending_data={"candidate_ids": [event.id for event in rows]},
            )
            return ChatOutput(
                answer="Нашёл несколько подходящих событий. Укажи, после какого показать дела — номер или ID:\n"
                       + "\n".join(f"{index}. {pretty(event)}" for index, event in enumerate(rows, 1)),
                session_id=sid, action="clarify", events=rows,
            )
        return self._after_event_by_id(rows[0].id, sid)

    def _after_event_by_id(self, event_id: int, sid: str) -> ChatOutput:
        anchor, later, total = self.planner.list_after(event_id, limit=100)
        self.planner.set_session(sid, active_event_id=anchor.id)
        if anchor.scheduled_date is None or anchor.scheduled_time is None:
            return ChatOutput(
                answer=f"У «{anchor.title}» (#{anchor.id}) не указаны дата и время, "
                       "поэтому я не могу определить дела после него.",
                session_id=sid, action="after_event", event=anchor,
            )
        reference = (f"«{anchor.title}» (#{anchor.id}, "
                     f"{anchor.scheduled_date.strftime('%d.%m.%Y')} в "
                     f"{anchor.scheduled_time.strftime('%H:%M')})")
        if not later:
            answer = f"После {reference} в тот день ничего не запланировано."
        else:
            answer = f"После {reference} в тот день:\n" + "\n".join(pretty(event) for event in later)
            if total > len(later):
                answer += f"\nПоказаны первые {len(later)} из {total}."
        return ChatOutput(answer=answer, session_id=sid, action="after_event",
                          event=anchor, events=later)

    def _create_from_freeform(self, parsed: ExtractedEvent, sid: str, state: dict) -> ChatOutput:
        """Создаёт событие (или серию событий при повторении) из распознанного текста."""
        now = datetime.now(ZoneInfo(self.planner.default_timezone))
        kind = parsed.kind or Kind.task
        duration = parsed.duration_minutes or (60 if kind == Kind.meeting else None)

        # Поддерживаем повторение: "всю неделю" — событие на каждый день недели
        dates: list[date | None] = []
        if parsed.recurrence == "daily" and parsed.start_date and parsed.end_date:
            d = parsed.start_date
            while d <= parsed.end_date:
                dates.append(d)
                d += timedelta(days=1)
        elif parsed.start_date:
            dates = [parsed.start_date]
        elif parsed.recurrence is None:
            # A task without a deadline is valid and still needs one create call.
            dates = [None]

        if not dates:
            return ChatOutput(answer="Не удалось определить дату начала повторения. Уточни её, пожалуйста.",
                              session_id=sid, action="clarify")

        created: list[Event] = []
        for d in dates:
            payload = EventCreate(
                kind=kind,
                title=parsed.title or "Событие",
                scheduled_date=d,
                scheduled_time=parsed.start_time,
                duration_minutes=duration,
            )
            event = self.planner.create(payload)
            created.append(event)

        self.planner.set_session(sid, active_event_id=created[-1].id)
        label = "События созданы" if len(created) > 1 else "Событие создано"
        answer = f"{label} ({len(created)}):\n" + "\n".join(pretty(e) for e in created)
        logger.info(
            "Событие создано из свободной формы",
            extra={"session_id": sid, "action": "freeform_create",
                   "kind": kind.value, "title": parsed.title,
                   "recurrence": parsed.recurrence, "dates_count": len(dates),
                   "event_ids": [e.id for e in created]},
        )
        return ChatOutput(answer=answer, session_id=sid, action="create",
                          events=created, needs_llm=False)

    def _query_from_freeform(self, parsed: ExtractedEvent, sid: str, state: dict) -> ChatOutput:
        """Отвечает на вопрос о существующих событиях (список / поиск)."""
        from_date = parsed.start_date
        to_date = parsed.end_date

        # Если нет ни названия, ни даты — уточняем, о чём спрашивает пользователь
        if not parsed.title and not from_date:
            return ChatOutput(
                answer="О чём именно спросить? Например: «что запланировано на завтра?», «есть ли встречи в пятницу?»",
                session_id=sid, action="clarify", needs_llm=False,
            )

        # Для вопросов типа "в тот день" без конкретной даты — ищем по имени события из заголовка
        if parsed.title and not from_date:
            rows, total = self.planner.search(parsed.title, kind=parsed.kind, limit=20)
            events = [Event.model_validate(r) for r in rows]
            if not events:
                answer = f"Не нашёл событий по запросу «{parsed.title}»."
            else:
                answer = f"Найдено по запросу «{parsed.title}»:\n" + "\n".join(pretty(e) for e in events)
            logger.info("FreeFormParser поиск по названию",
                        extra={"session_id": sid, "action": "freeform_query",
                               "query": parsed.title, "kind": parsed.kind.value if parsed.kind else None,
                               "results_count": len(events)})
            return ChatOutput(answer=answer, session_id=sid, action="search",
                              events=events, needs_llm=False)

        # Обычный список событий за период
        items, total = self.planner.list(from_date=from_date, to_date=to_date,
                                         kind=parsed.kind, limit=50)
        events = [Event.model_validate(r) for r in items]

        if not events:
            period_txt = ""
            if from_date and to_date:
                period_txt = f" с {from_date} по {to_date}"
            elif from_date:
                period_txt = f" на {from_date}"
            answer = f"На этот период{period_txt} событий нет."
        else:
            period_txt = ""
            if from_date and to_date:
                period_txt = f" с {from_date} по {to_date}"
            elif from_date:
                period_txt = f" на {from_date}"
            answer = f"События{period_txt}:\n" + "\n".join(pretty(e) for e in events)

        logger.info("FreeFormParser запрос списка",
                    extra={"session_id": sid, "action": "freeform_query",
                           "from_date": str(from_date) if from_date else None,
                           "to_date": str(to_date) if to_date else None,
                           "kind": parsed.kind.value if parsed.kind else None,
                           "results_count": len(events)})
        return ChatOutput(answer=answer, session_id=sid, action="list",
                          events=events, needs_llm=False)

    def _maybe_describe(self, raw: str, result: ChatOutput, sid: str, state: dict) -> ChatOutput:
        """Hybrid responses: LLM may phrase reads, never mutation confirmations."""
        describe = getattr(self.provider, "describe", None)
        if not self.llm_configured or not callable(describe):
            return result
        if result.action not in {"list", "search", "get", "list_events", "search_events", "get_event"}:
            return result
        events = result.events or ([result.event] if result.event else [])
        if not events:
            return result
        payload = {"action": result.action, "count": len(events),
                   "items": [event.model_dump(mode="json") for event in events]}
        try:
            answer = describe(raw, payload, self._context(sid, state))
        except Exception:
            # The actual read result is already available; deterministic text is
            # safer than repeating the request or discarding the records.
            return result
        if isinstance(answer, str) and answer.strip():
            result.answer = answer.strip()
            result.mode = "deepseek"
        return result

    def _search_with_synonyms(self, query: str, sid: str, state: dict) -> ChatOutput:
        # Do not pass complete raw commands to search; this step is reserved for
        # plain query strings. Rechecking original terms is harmless if unmatched.
        logger.info("Поиск синонимов через LLM",
                    extra={"session_id": sid, "action": "search_synonyms", "query": query})
        start = time.perf_counter()
        try:
            variants = self.provider.expand_search(query, self._context(sid, state))
        except Exception:
            latency_ms = (time.perf_counter() - start) * 1000
            logger.exception("Ошибка получения синонимов от LLM",
                             extra={"session_id": sid, "action": "search_synonyms",
                                    "query": query, "llm_latency_ms": round(latency_ms, 2)})
            variants = []
        if not isinstance(variants, list):
            variants = []
        latency_ms = (time.perf_counter() - start) * 1000
        logger.info("Синонимы получены",
                    extra={"session_id": sid, "action": "search_synonyms", "query": query,
                           "variants": variants, "variants_count": len(variants),
                           "llm_latency_ms": round(latency_ms, 2)})
        found: dict[int, Event] = {}
        for phrase in variants[:5]:
            if not isinstance(phrase, str) or not (2 <= len(phrase.strip()) <= 500):
                continue
            rows, _ = self.planner.search(phrase, limit=8)
            for row in rows:
                found[row.id] = row
        events = list(found.values())[:15]
        if len(events) == 1:
            self.planner.set_session(sid, active_event_id=events[0].id)
        answer = "Ничего подходящего не нашлось. Уточни название события." if not events else \
            "Найдено по похожим формулировкам:\n" + "\n".join(pretty(e) for e in events)
        logger.info("Поиск по синонимам завершён",
                    extra={"session_id": sid, "action": "search_synonyms", "query": query,
                           "results_count": len(events), "mode": "deepseek" if variants else "fallback"})
        return ChatOutput(answer=answer, session_id=sid, action="search", events=events,
                          mode="deepseek" if variants else "fallback")
