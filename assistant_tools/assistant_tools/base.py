from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .contracts import ToolExecutionError, ToolResult
from .validation import validate_schema


class BaseTool(ABC):
    name: str
    description: str
    parameters: dict[str, Any]

    def definition(self) -> dict[str, Any]:
        """Provider-neutral function/tool definition."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    def invoke(self, arguments: dict[str, Any]) -> ToolResult:
        if not isinstance(arguments, dict):
            return ToolResult.fail("VALIDATION_ERROR", "Аргументы инструмента должны быть JSON-объектом.")

        validation_error = validate_schema(self.parameters, arguments)
        if validation_error:
            return ToolResult.fail("VALIDATION_ERROR", validation_error)

        try:
            return ToolResult.ok(self.execute(arguments))
        except ToolExecutionError as exc:
            return ToolResult.fail(exc.code, exc.message)
        except ValueError as exc:
            return ToolResult.fail("VALIDATION_ERROR", str(exc))
        except Exception:
            # Do not expose database/provider internals to the model or end user.
            return ToolResult.fail("INTERNAL_ERROR", "Не удалось выполнить инструмент из-за внутренней ошибки.")

    @abstractmethod
    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError
