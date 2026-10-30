from __future__ import annotations

_EVENT_TYPES = ["task", "reminder", "meeting"]
_STATUSES = ["pending", "completed", "cancelled"]
_NULLABLE_STRING = {"type": ["string", "null"]}
_ISO_DATE = {"type": ["string", "null"], "pattern": r"^\d{4}-\d{2}-\d{2}$"}
_ISO_DATETIME = {
    "type": "string",
    "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?$",
}


def _object(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


CREATE_TASK = _object(
    {
        "title": {"type": "string", "minLength": 1, "description": "Краткое название задачи."},
        "description": {**_NULLABLE_STRING, "description": "Подробности задачи или null."},
        "due_date": {**_ISO_DATE, "description": "Срок без конкретного времени, YYYY-MM-DD, либо null."},
        "due_at": {"type": ["string", "null"], "pattern": _ISO_DATETIME["pattern"], "description": "Точный срок ISO 8601 с часовым поясом либо null."},
    },
    ["title", "description", "due_date", "due_at"],
)

CREATE_REMINDER = _object(
    {
        "title": {"type": "string", "minLength": 1, "description": "Текст напоминания."},
        "description": {**_NULLABLE_STRING, "description": "Дополнительные сведения или null."},
        "remind_at": {**_ISO_DATETIME, "description": "Когда напомнить: ISO 8601 с часовым поясом."},
    },
    ["title", "description", "remind_at"],
)

CREATE_MEETING = _object(
    {
        "title": {"type": "string", "minLength": 1, "description": "Название встречи."},
        "description": {**_NULLABLE_STRING, "description": "Дополнительные сведения или null."},
        "scheduled_at": {**_ISO_DATETIME, "description": "Начало встречи: ISO 8601 с часовым поясом."},
        "end_at": {"type": ["string", "null"], "pattern": _ISO_DATETIME["pattern"], "description": "Окончание встречи ISO 8601 с часовым поясом или null."},
    },
    ["title", "description", "scheduled_at", "end_at"],
)

LIST_EVENTS = _object(
    {
        "start_at": {**_ISO_DATETIME, "description": "Начало периода включительно."},
        "end_at": {**_ISO_DATETIME, "description": "Конец периода исключительно."},
        "types": {"type": "array", "items": {"type": "string", "enum": _EVENT_TYPES}, "uniqueItems": True, "description": "Типы записей; пустой массив означает все типы."},
        "status": {"type": ["string", "null"], "enum": _STATUSES + [None], "description": "Статус или null для всех статусов."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    },
    ["start_at", "end_at", "types", "status", "limit"],
)

SEARCH_EVENTS = _object(
    {
        "query": {"type": "string", "minLength": 1, "description": "Исходная формулировка пользователя для лексического поиска."},
        "query_variants": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 200},
            "maxItems": 5,
            "uniqueItems": True,
            "description": "До пяти альтернативных формулировок. Заполняй ТОЛЬКО при повторном вызове после того, как первый поиск вернул 0 результатов. При первом вызове не передавай это поле или передай пустой массив."
        },
        "types": {"type": "array", "items": {"type": "string", "enum": _EVENT_TYPES}, "uniqueItems": True, "description": "Типы записей; пустой массив означает все типы."},
        "status": {"type": ["string", "null"], "enum": _STATUSES + [None]},
        "date_from": {**_ISO_DATE, "description": "Нижняя граница даты события включительно или null."},
        "date_to": {**_ISO_DATE, "description": "Верхняя граница даты события включительно или null."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    },
    ["query", "types", "status", "date_from", "date_to", "limit"],
)
