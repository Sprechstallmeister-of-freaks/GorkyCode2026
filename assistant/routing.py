"""Rule-first dialogue orchestration with a replaceable LLM provider.

The database remains the source of truth. Model suggestions always pass through
ToolRegistry validation and Planner domain rules. Session state handles follow-ups.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from .contracts import LLMContext, ParsedCommand
from .dates import parse_datetime
from .dialogue import RuleDialogue, pretty
from .llm_provider import LLMProvider, NullLLMProvider
from .models import ChatOutput, Event
from .service import Planner
from .tools import ToolRegistry, DEFINITIONS

CANCEL_WORDS = {"отмена", "отмени", "стоп", "не надо", "передумал"}
YES_WORDS = {"да", "подтверждаю", "подтвердить", "да, отменяй", "отменяй"}
NO_WORDS = {"нет", "не отменяй", *CANCEL_WORDS}
ORDINALS = {"первую": 0, "первый": 0, "первое": 0, "1": 0,
            "вторую": 1, "второй": 1, "второе": 1, "2": 1,
            "третью": 2, "третий": 2, "третье": 2, "3": 2}


class ChatRouter:
    def __init__(self, planner: Planner, *, provider: LLMProvider | None = None,
                 rules: RuleDialogue | None = None):
        self.planner = planner
        self.rules = rules or RuleDialogue(planner)
        self.provider = provider or NullLLMProvider()
        self.tools = ToolRegistry(planner)
        self.llm_configured = not isinstance(self.provider, NullLLMProvider)

    @property
    def llm_connected(self) -> bool:
        return bool(getattr(self.provider, "connected", self.llm_configured))

    def process(self, message: str, session_id: str) -> ChatOutput:
        """Exactly one user+assistant history pair per incoming message."""
        raw = message.strip()
        state = self.planner.get_session(session_id)
        try:
            if state.get("pending_action") in {"select_event", "confirm_cancel", "complete_command"}:
                result = self._continue(raw, session_id, state)
            else:
                # Existing rule handler retains its own create-event clarification.
                result = self.rules.process(raw, session_id, record=False)
                if result.needs_llm:
                    result = self._interpret(raw, session_id, state)
                elif result.action == "search" and not result.events:
                    result = self._search_with_synonyms(raw, session_id, state)
                result = self._maybe_describe(raw, result, session_id, state)
        except (ValueError, TypeError, ValidationError) as exc:
            result = ChatOutput(answer=f"Некорректные параметры: {exc}", session_id=session_id, action="error")
        self.planner.append_dialogue(session_id, raw, result.answer)
        return result

    def _context(self, sid: str, state: dict | None = None) -> LLMContext:
        # The model sees ONLY wall-clock time and the configured IANA zone.
        # Session state is deliberately never included in its JSON context.
        zone = self.planner.default_timezone
        return LLMContext(now=datetime.now(ZoneInfo(zone)), timezone=zone)

    def _interpret(self, text: str, sid: str, state: dict) -> ChatOutput:
        try:
            command = ParsedCommand.model_validate(self.provider.interpret(text, self._context(sid, state), self.tools.specs()))
        except Exception:
            return ChatOutput(answer="Не удалось обработать свободную формулировку. Попробуй перефразировать запрос.",
                              session_id=sid, action="unrecognized", needs_llm=True)
        if command.tool == "unknown":
            return ChatOutput(answer="Не удалось определить действие. Уточни, что нужно создать, найти или изменить.",
                              session_id=sid, action="unrecognized", needs_llm=True)
        result = self._execute(command, sid, state)
        result.mode = "deepseek"
        return result

    def _execute(self, command: ParsedCommand, sid: str, state: dict, *, confirmed: bool = False) -> ChatOutput:
        name = command.tool
        if name == "unknown":
            return ChatOutput(answer="Не удалось определить действие.", session_id=sid, action="unrecognized")
        args = dict(command.arguments)
        if name in {"update_event", "cancel_event", "get_event"} and not args.get("event_id"):
            if command.target_query:
                rows, _ = self.planner.search(command.target_query, limit=8)
                if not rows:
                    return ChatOutput(answer="Не нашёл подходящее событие. Уточни его название.", session_id=sid, action="not_found")
                if len(rows) > 1:
                    self.planner.set_session(sid, active_event_id=state.get("active_event_id"),
                        pending_action="select_event", pending_data={"tool": name, "arguments": args,
                        "candidate_ids": [e.id for e in rows], "candidate_versions": [e.version for e in rows]})
                    return ChatOutput(answer="Нашёл несколько событий. Укажи номер или ID:\n" + "\n".join(
                        f"{idx}. {pretty(e)}" for idx, e in enumerate(rows, 1)),
                        session_id=sid, action="clarify", events=rows)
                args["event_id"] = rows[0].id
            elif state.get("active_event_id"):
                args["event_id"] = state["active_event_id"]
            else:
                return ChatOutput(answer="Какое событие имеется в виду? Укажи ID или название.", session_id=sid, action="clarify")

        if name == "create_event":
            kind = args.get("kind")
            missing = [field for field in ("title", "scheduled_date", "scheduled_time")
                       if not args.get(field) and (field == "title" or kind in {"meeting", "reminder"})]
            if missing:
                self.planner.set_session(sid, active_event_id=state.get("active_event_id"),
                    pending_action="complete_command", pending_data={"tool": name, "arguments": args})
                return ChatOutput(answer=self._missing_question(missing[0]), session_id=sid, action="clarify")

        if name == "cancel_event" and not confirmed:
            try:
                event = self.planner.get(int(args["event_id"]))
            except (ValueError, TypeError):
                return ChatOutput(answer="Неверный ID события.", session_id=sid, action="error")
            except Exception as exc:
                from .service import NotFoundError
                if isinstance(exc, NotFoundError):
                    return ChatOutput(answer=str(exc), session_id=sid, action="not_found")
                raise
            # Pin the existing version so stale confirmations cannot cancel updated records.
            self.planner.set_session(sid, active_event_id=state.get("active_event_id"),
                pending_action="confirm_cancel", pending_data={"tool": name,
                    "arguments": {**args, "expected_version": event.version}})
            return ChatOutput(answer=f"Подтверди отмену «{event.title}» (#{event.id}): да или нет?",
                              session_id=sid, action="clarify")

        # Extra/untrusted keys cannot leak into the tools. Registry handles validation.
        outcome = self.tools.execute(name, args, confirmed_by_user=confirmed)
        if not outcome["ok"]:
            code = outcome.get("error", "error")
            action = "conflict" if code == "conflict" else "not_found" if code == "not_found" else "error"
            return ChatOutput(answer=outcome.get("message", "Ошибка выполнения"), session_id=sid, action=action)

        if "event" in outcome:
            event = Event.model_validate(outcome["event"])
            self.planner.set_session(sid, active_event_id=event.id)
            verb = {"create_event": "Создано", "update_event": "Изменено", "cancel_event": "Отменено", "get_event": "Событие"}[name]
            return ChatOutput(answer=f"{verb}: {pretty(event)}", session_id=sid,
                              action=name, event=event)

        events = [Event.model_validate(item) for item in outcome.get("items", [])]
        if name == "search_events" and not events:
            return self._search_with_synonyms(args.get("query", ""), sid, state)
        if len(events) == 1 and name == "search_events":
            self.planner.set_session(sid, active_event_id=events[0].id)
        total = outcome.get("total", len(events))
        label = "Найдено" if name == "search_events" else "Планы"
        answer = "Ничего не найдено." if not events else f"{label}:\n" + "\n".join(pretty(e) for e in events)
        if total > len(events):
            answer += f"\nИ ещё {total - len(events)} записей."
        return ChatOutput(answer=answer, session_id=sid, action=name, events=events)

    @staticmethod
    def _missing_question(field: str) -> str:
        return {"title": "Что записать? Назови событие.",
                "scheduled_date": "На какую дату? Например, «завтра».",
                "scheduled_time": "На какое время? Например, «15:00»."}.get(field, f"Уточни {field}.")

    def _continue(self, raw: str, sid: str, state: dict) -> ChatOutput:
        normalized = raw.lower().replace("ё", "е").strip(" .!?")
        if normalized in CANCEL_WORDS:
            self.planner.set_session(sid, active_event_id=state.get("active_event_id"))
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
        return ChatOutput(answer="Неизвестное состояние диалога.", session_id=sid, action="error")

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
        try:
            variants = self.provider.expand_search(query, self._context(sid, state))
        except Exception:
            variants = []
        if not isinstance(variants, list):
            variants = []
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
        return ChatOutput(answer=answer, session_id=sid, action="search", events=events,
                          mode="deepseek" if variants else "fallback")
