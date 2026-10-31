"""Централизованная настройка логирования для API сервиса.

Принципы:
* Читаемые человеком текстовые логи на русском языке.
* request_id сквозь весь запрос — корреляция HTTP -> сервис -> LLM -> БД.
* Маскировка секретов (API-ключи, Authorization) — не попадают в логи.
* Уровень и файл задаются через переменные окружения.
"""

from __future__ import annotations

import logging
import logging.config
import os
import re
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

# Scoped request id: корреляция логов в рамках одного HTTP-запроса.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id() -> str | None:
    return request_id_var.get()


def set_request_id(value: str | None) -> None:
    request_id_var.set(value)


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


class RequestIdFilter(logging.Filter):
    """Добавляет request_id в каждую запись лога."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id() or "-"
        return True


# Служебные атрибуты LogRecord, которые не выводим как поля (шум).
_LOGRECORD_ATTRS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
    "request_id",
}


class HumanFormatter(logging.Formatter):
    """Читаемый текстовый форматтер.

    Пример:
    [2026-10-31 13:05:12] [INFO] [assistant.api] [req:a1b2c3d4] Входящий HTTP-запрос | method=POST path=/api/chat session_id=demo
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        level = record.levelname
        name = record.name
        rid = getattr(record, "request_id", "-")
        msg = record.getMessage()

        # Добавляем дополнительные поля (extra=...) в читаемом виде.
        extra_parts: list[str] = []
        for key, value in sorted(record.__dict__.items()):
            if key in _LOGRECORD_ATTRS:
                continue
            if value is None:
                continue
            extra_parts.append(f"{key}={value}")
        extra_str = (" | " + " ".join(extra_parts)) if extra_parts else ""

        if record.exc_info:
            msg = f"{msg}\n{self.formatException(record.exc_info)}"

        return f"[{ts}] [{level}] [{name}] [req:{rid}] {msg}{extra_str}"


def configure_logging(
    level: str | None = None,
    log_file: str | None = None,
) -> None:
    """Инициализировать логирование.

    Args:
        level: уровень логирования (по умолчанию из ASSISTANT_LOG_LEVEL, INFO).
        log_file: опциональный путь к файлу с ротацией (из ASSISTANT_LOG_FILE).
    """
    level = (level or os.getenv("ASSISTANT_LOG_LEVEL", "INFO")).upper()
    log_file = log_file or os.getenv("ASSISTANT_LOG_FILE")

    handlers: list[str] = ["console"]
    handler_configs: dict[str, Any] = {
        "console": {
            "class": "logging.StreamHandler",
            "stream": sys.stdout,
            "formatter": "human",
            "filters": ["request_id"],
        },
    }
    if log_file:
        handlers.append("file")
        handler_configs["file"] = {
            "class": "logging.handlers.TimedRotatingFileHandler",
            "filename": log_file,
            "when": "D",
            "interval": 1,
            "backupCount": 7,
            "encoding": "utf-8",
            "formatter": "human",
            "filters": ["request_id"],
        }

    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "human": {
                "()": HumanFormatter,
            },
        },
        "filters": {
            "request_id": {
                "()": RequestIdFilter,
            },
        },
        "handlers": handler_configs,
        "root": {
            "level": level,
            "handlers": handlers,
        },
        "loggers": {
            "assistant": {
                "level": level,
                "handlers": handlers,
                "propagate": False,
            },
            "uvicorn": {
                "level": "WARNING",
                "handlers": handlers,
                "propagate": False,
            },
            "uvicorn.access": {
                "level": "WARNING",
                "handlers": [],
                "propagate": False,
            },
            "httpx": {
                "level": "WARNING",
                "handlers": handlers,
                "propagate": False,
            },
        },
    })


def mask_sensitive(value: str | None) -> str | None:
    """Заменить секреты в строке на *** (Authorization, API-ключи)."""
    if value is None:
        return None
    masked = re.sub(r"(Authorization\s*:\s*)[^\s,;]+", r"\1***", value, flags=re.I)
    masked = re.sub(r"(api.?key\s*=\s*)[^\s,;]+", r"\1***", masked, flags=re.I)
    masked = re.sub(r"(key\s*[:=]\s*)[^\s,;]{8,}", r"\1***", masked, flags=re.I)
    return masked


def safe_body(body: bytes | None, max_chars: int = 2000) -> str | None:
    """Безопасное представление тела запроса для лога (обрезано, секреты замащены)."""
    if body is None:
        return None
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return "<binary>"
    text = mask_sensitive(text)
    if len(text) > max_chars:
        text = text[:max_chars] + f"... ({len(text) - max_chars} chars truncated)"
    return text
