from __future__ import annotations

from datetime import date, datetime

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError


_FORMAT_CHECKER = FormatChecker()


def validate_schema(schema: dict, arguments: dict) -> str | None:
    validator = Draft202012Validator(schema, format_checker=_FORMAT_CHECKER)
    error = next(iter(validator.iter_errors(arguments)), None)
    if error is None:
        return None
    path = ".".join(str(part) for part in error.absolute_path)
    prefix = f"{path}: " if path else ""
    return prefix + error.message


def parse_aware_datetime(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Поле {field_name} должно быть датой и временем в формате ISO 8601.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Поле {field_name} должно содержать часовой пояс, например +03:00 или Z.")
    return parsed


def parse_iso_date(value: str, field_name: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Поле {field_name} должно быть датой в формате YYYY-MM-DD.") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"Поле {field_name} должно быть в формате YYYY-MM-DD.")
    return parsed
