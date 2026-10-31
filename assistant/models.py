from __future__ import annotations

from datetime import date, datetime, time
from enum import Enum
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Kind(str, Enum):
    task = "task"
    reminder = "reminder"
    meeting = "meeting"


class Status(str, Enum):
    open = "open"
    done = "done"
    cancelled = "cancelled"


class EventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Kind
    title: str = Field(min_length=1, max_length=500)
    scheduled_date: date | None = None
    scheduled_time: time | None = None
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    timezone: str | None = None

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Title cannot be blank")
        return value

    @field_validator("scheduled_time")
    @classmethod
    def minute_precision(cls, value: time | None) -> time | None:
        if value is not None and (value.second or value.microsecond or value.tzinfo):
            raise ValueError("Time must be HH:MM without timezone")
        return value

    @model_validator(mode="after")
    def coherent_date(self):
        if self.scheduled_time and not self.scheduled_date:
            raise ValueError("A time requires a date")
        if self.kind == Kind.meeting and (self.scheduled_date is None or self.scheduled_time is None):
            raise ValueError("A meeting needs both date and time")
        if self.kind == Kind.reminder and (self.scheduled_date is None or self.scheduled_time is None):
            raise ValueError("A reminder needs both date and time")
        if self.duration_minutes is not None and self.kind != Kind.meeting:
            raise ValueError("Only meetings can have a duration")
        return self


class EventPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=500)
    scheduled_date: date | None = None
    scheduled_time: time | None = None
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    status: Status | None = None
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator("title")
    @classmethod
    def title_normalize(cls, value: str | None):
        if value is None:
            return value
        result = " ".join(value.split())
        if not result:
            raise ValueError("Title cannot be blank")
        return result

    @field_validator("scheduled_time")
    @classmethod
    def minute_precision(cls, value: time | None):
        if value and (value.second or value.microsecond or value.tzinfo):
            raise ValueError("Time must be HH:MM")
        return value


class Event(BaseModel):
    id: int
    kind: Kind
    title: str
    scheduled_date: date | None
    scheduled_time: time | None
    duration_minutes: int | None
    timezone: str
    status: Status
    created_at: datetime
    updated_at: datetime
    source: str
    version: int
    notified_at: datetime | None = None


class EventList(BaseModel):
    items: list[Event]
    total: int


class SearchResponse(BaseModel):
    items: list[Event]
    total: int
    strategy: Literal["lexical_fuzzy"] = "lexical_fuzzy"


class NotificationItem(BaseModel):
    id: int
    kind: Kind
    title: str
    scheduled_date: date | None
    scheduled_time: time | None
    timezone: str


class NotificationsDueResponse(BaseModel):
    items: list[NotificationItem]
    count: int


class ChatInput(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str = Field(default="default", min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")


class ChatOutput(BaseModel):
    answer: str
    session_id: str
    action: str
    event: Event | None = None
    events: list[Event] = Field(default_factory=list)
    needs_llm: bool = False
    mode: Literal["deepseek", "fallback"] = "fallback"
