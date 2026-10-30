from __future__ import annotations

from typing import Any

from .base import BaseTool
from .contracts import ToolExecutionError
from .ports import EventRepository, EventSearchService
from .schemas import CREATE_MEETING, CREATE_REMINDER, CREATE_TASK, LIST_EVENTS, SEARCH_EVENTS
from .validation import parse_aware_datetime, parse_iso_date


def _clean_title(arguments: dict[str, Any]) -> str:
    title = arguments["title"].strip()
    if not title:
        raise ToolExecutionError("VALIDATION_ERROR", "title не должен быть пустым.")
    return title


def _create_event(repository: EventRepository, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return repository.create_event(payload)
    except Exception as exc:
        raise ToolExecutionError("STORAGE_ERROR", "Не удалось сохранить запись.") from exc


class CreateTaskTool(BaseTool):
    name = "create_task"
    description = (
        "Создаёт задачу. due_date используется для срока без времени, due_at — для точного момента. "
        "Оба поля должны быть null, если срок не указан. Не придумывай срок, которого пользователь не называл."
    )
    parameters = CREATE_TASK

    def __init__(self, repository: EventRepository) -> None:
        self.repository = repository

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        due_date = arguments["due_date"]
        due_at = arguments["due_at"]
        if due_date is not None and due_at is not None:
            raise ToolExecutionError("VALIDATION_ERROR", "Укажи либо due_date, либо due_at. Одновременно оба поля задавать нельзя.")
        if due_date is not None:
            parse_iso_date(due_date, "due_date")
        if due_at is not None:
            parse_aware_datetime(due_at, "due_at")
        payload = {**arguments, "title": _clean_title(arguments), "type": "task"}
        return {"event": _create_event(self.repository, payload)}


class CreateReminderTool(BaseTool):
    name = "create_reminder"
    description = (
        "Создаёт напоминание на точный момент времени. remind_at обязателен и должен включать часовой пояс. "
        "Если время не названо, сначала запроси его у пользователя."
    )
    parameters = CREATE_REMINDER

    def __init__(self, repository: EventRepository) -> None:
        self.repository = repository

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        parse_aware_datetime(arguments["remind_at"], "remind_at")
        payload = {**arguments, "title": _clean_title(arguments), "type": "reminder"}
        return {"event": _create_event(self.repository, payload)}


class CreateMeetingTool(BaseTool):
    name = "create_meeting"
    description = (
        "Создаёт встречу. Время начала обязательно и должно включать часовой пояс. "
        "Время окончания можно не указывать. Не придумывай дату или время."
    )
    parameters = CREATE_MEETING

    def __init__(self, repository: EventRepository) -> None:
        self.repository = repository

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        start = parse_aware_datetime(arguments["scheduled_at"], "scheduled_at")
        end_value = arguments["end_at"]
        if end_value is not None:
            end = parse_aware_datetime(end_value, "end_at")
            if end <= start:
                raise ToolExecutionError("VALIDATION_ERROR", "end_at должен быть позже scheduled_at.")
        payload = {**arguments, "title": _clean_title(arguments), "type": "meeting"}
        return {"event": _create_event(self.repository, payload)}


class ListEventsTool(BaseTool):
    name = "list_events"
    description = (
        "Возвращает записи в заданном периоде [start_at, end_at). "
        "Для задач учитывается due_date/due_at, для напоминаний remind_at, для встреч scheduled_at. "
        "Задачи без срока в период не входят. Пустой types означает все типы."
    )
    parameters = LIST_EVENTS

    def __init__(self, repository: EventRepository) -> None:
        self.repository = repository

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        start = parse_aware_datetime(arguments["start_at"], "start_at")
        end = parse_aware_datetime(arguments["end_at"], "end_at")
        if end <= start:
            raise ToolExecutionError("VALIDATION_ERROR", "end_at должен быть позже start_at.")

        filters = {
            "start_at": start,
            "end_at": end,
            "types": arguments["types"] or None,
            "status": arguments["status"],
            "limit": arguments["limit"],
        }
        try:
            events = self.repository.list_events(filters)
        except Exception as exc:
            raise ToolExecutionError("STORAGE_ERROR", "Не удалось получить записи из хранилища.") from exc
        return {"events": events[: arguments["limit"]], "count": min(len(events), arguments["limit"])}


class SearchEventsTool(BaseTool):
    name = "search_events"
    description = (
        "Ищет ранее созданные записи по тексту, включая завершённые и старые. "
        "При первом вызове используй исходную формулировку и НЕ генерируй синонимы заранее. "
        "Если первый вызов вернул count=0, один раз повтори вызов: передай до пяти альтернативных "
        "формулировок в query_variants. Не повторяй поиск бесконечно. "
        "date_from/date_to фильтруют по дате самого события, а не по created_at. "
        "Пустой types означает все типы. relevance — оценка ранжирования, не вероятность."
    )
    parameters = SEARCH_EVENTS

    def __init__(self, search_service: EventSearchService) -> None:
        self.search_service = search_service

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = arguments["query"].strip()
        if not query:
            raise ToolExecutionError("VALIDATION_ERROR", "query не должен быть пустым.")

        date_from = arguments["date_from"]
        date_to = arguments["date_to"]
        if date_from is not None:
            parse_iso_date(date_from, "date_from")
        if date_to is not None:
            parse_iso_date(date_to, "date_to")
        if date_from is not None and date_to is not None and date_from > date_to:
            raise ToolExecutionError("VALIDATION_ERROR", "date_to не может быть раньше date_from.")

        filters = {
            "types": arguments["types"] or None,
            "status": arguments["status"],
            "date_from": date_from,
            "date_to": date_to,
        }
        try:
            query_variants = arguments.get("query_variants") or []
            events = self.search_service.search(
                query=query,
                filters=filters,
                limit=arguments["limit"],
                query_variants=query_variants,
            )
        except Exception as exc:
            raise ToolExecutionError("SEARCH_ERROR", "Не удалось выполнить поиск по записям.") from exc
        return {"events": events, "count": len(events)}
