"""Stable, strictly validated tool contract for a future LLM adapter.

The model will only select a tool and provide JSON arguments. It never sees SQL,
filesystem paths or arbitrary Python execution. Errors return structured results.
"""
from __future__ import annotations

from datetime import date, time
from typing import Callable
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .models import EventCreate, EventPatch, Kind, Status
from .service import Planner, DomainError, ConflictError, NotFoundError


class StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateArgs(StrictArgs):
    kind: Kind
    title: str = Field(min_length=1, max_length=500)
    scheduled_date: date | None = None
    scheduled_time: time | None = None
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)


class ListArgs(StrictArgs):
    from_date: date | None = None
    to_date: date | None = None
    kind: Kind | None = None
    limit: int = Field(default=30, ge=1, le=100)


class SearchArgs(StrictArgs):
    query: str = Field(min_length=2, max_length=500)
    kind: Kind | None = None
    limit: int = Field(default=10, ge=1, le=30)


class UpdateArgs(StrictArgs):
    event_id: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    scheduled_date: date | None = None
    scheduled_time: time | None = None
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    status: Status | None = None
    expected_version: int | None = Field(default=None, ge=1)


class CancelArgs(StrictArgs):
    event_id: int = Field(ge=1)
    expected_version: int | None = Field(default=None, ge=1)


class GetArgs(StrictArgs):
    event_id: int = Field(ge=1)


DEFINITIONS: dict[str, tuple[type[BaseModel], str]] = {
    "create_event": (CreateArgs, "Создать задачу, напоминание или встречу. Для встречи и напоминания обязательно указать дату и время."),
    "list_events": (ListArgs, "Получить список событий за период. Даты включительно."),
    "search_events": (SearchArgs, "Найти ранее созданные записи по содержанию, без точного совпадения текста."),
    "get_event": (GetArgs, "Получить существующее событие по идентификатору."),
    "update_event": (UpdateArgs, "Изменить событие, время или статус. Использовать event_id из get/search, а не придумывать."),
    "cancel_event": (CancelArgs, "Отменить событие. Runtime ОБЯЗАН получить подтверждение пользователя вне аргументов модели."),
}


class ToolRegistry:
    def __init__(self, planner: Planner):
        self.planner = planner

    def specs(self) -> list[dict]:
        return [{"type": "function", "function": {"name": name, "description": description,
                "parameters": schema.model_json_schema()}}
                for name, (schema, description) in DEFINITIONS.items()]

    def execute(self, name: str, arguments: dict, *, confirmed_by_user: bool = False) -> dict:
        if name not in DEFINITIONS:
            return {"ok": False, "error": "unknown_tool", "message": f"Нет инструмента {name}"}
        schema, _ = DEFINITIONS[name]
        try:
            args = schema.model_validate(arguments)
            if name == "create_event":
                event = self.planner.create(EventCreate.model_validate(args.model_dump()))
                return {"ok": True, "event": event.model_dump(mode="json")}
            if name == "get_event":
                return {"ok": True, "event": self.planner.get(args.event_id).model_dump(mode="json")}
            if name == "list_events":
                items, total = self.planner.list(from_date=args.from_date, to_date=args.to_date,
                                                 kind=args.kind, limit=args.limit)
                return {"ok": True, "items": [e.model_dump(mode="json") for e in items], "total": total}
            if name == "search_events":
                items, total = self.planner.search(args.query, kind=args.kind, limit=args.limit)
                return {"ok": True, "items": [e.model_dump(mode="json") for e in items], "total": total}
            if name == "update_event":
                if args.status == Status.cancelled:
                    return {"ok": False, "error": "confirmation_required", "message": "Отмена события выполняется только через cancel_event с отдельным подтверждением пользователя"}
                patch = args.model_dump(exclude_unset=True, exclude={"event_id"})
                event = self.planner.update(args.event_id, EventPatch.model_validate(patch))
                return {"ok": True, "event": event.model_dump(mode="json")}
            if name == "cancel_event":
                if not confirmed_by_user:
                    return {"ok": False, "error": "confirmation_required", "message": "Нужно отдельное подтверждение от пользователя"}
                event = self.planner.cancel(args.event_id, expected_version=args.expected_version)
                return {"ok": True, "event": event.model_dump(mode="json")}
        except ValidationError as exc:
            return {"ok": False, "error": "invalid_arguments", "message": str(exc)}
        except NotFoundError as exc:
            return {"ok": False, "error": "not_found", "message": str(exc)}
        except ConflictError as exc:
            return {"ok": False, "error": "conflict", "message": str(exc)}
        except DomainError as exc:
            return {"ok": False, "error": "domain_error", "message": str(exc)}
        return {"ok": False, "error": "unsupported"}
