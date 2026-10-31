"""Model-agnostic interchange contracts. No SQL or model-specific payloads."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ToolName = Literal[
    "create_event", "list_events", "search_events", "get_event", "update_event", "cancel_event"
]


class ParsedCommand(BaseModel):
    """The only action representation accepted from any external LLM provider.

    target_query is only used for finding the event_id of update/cancel requests.
    Providers MUST NOT guess event identifiers. The backend resolves candidates.
    """
    model_config = ConfigDict(extra="forbid")
    tool: ToolName | Literal["unknown"]
    arguments: dict[str, Any] = Field(default_factory=dict)
    target_query: str | None = Field(default=None, min_length=2, max_length=500)


class LLMContext(BaseModel):
    """The ONLY metadata JSON supplied to an LLM provider.

    Text and the fixed tool catalog are passed separately. Server-side session
    identifiers, conversation history and internal event IDs stay on the server.
    """
    model_config = ConfigDict(extra="forbid")
    now: datetime
    timezone: str

    @field_validator("now")
    @classmethod
    def require_offset(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("now must contain a UTC offset")
        return value

    @field_validator("timezone")
    @classmethod
    def require_iana_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
            raise ValueError("timezone must be an IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def consistent_offset(self) -> "LLMContext":
        expected = self.now.astimezone(ZoneInfo(self.timezone)).utcoffset()
        if self.now.utcoffset() != expected:
            raise ValueError("now offset does not match timezone")
        return self


class RouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["rule", "llm", "pending", "none"]
    result: Literal["match", "partial", "no_match"]
