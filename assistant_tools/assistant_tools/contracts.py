from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolError:
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class ToolResult:
    success: bool
    data: dict[str, Any] | None = None
    error: ToolError | None = None

    @classmethod
    def ok(cls, data: dict[str, Any]) -> "ToolResult":
        return cls(success=True, data=data)

    @classmethod
    def fail(cls, code: str, message: str) -> "ToolResult":
        return cls(success=False, error=ToolError(code=code, message=message))

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "data": self.data,
            "error": self.error.to_dict() if self.error else None,
        }


class ToolExecutionError(Exception):
    """Expected, user-facing failure raised by a tool implementation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
