from __future__ import annotations

from typing import Any, Iterable

from .base import BaseTool
from .contracts import ToolResult


class ToolRegistry:
    """Stores tool implementations and dispatches calls by their public names."""

    def __init__(self, tools: Iterable[BaseTool] = ()) -> None:
        self._tools: dict[str, BaseTool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: BaseTool) -> None:
        if not tool.name or tool.name in self._tools:
            raise ValueError(f"Инструмент с именем {tool.name!r} уже зарегистрирован или имя пустое.")
        self._tools[tool.name] = tool

    def definitions(self) -> list[dict[str, Any]]:
        return [tool.definition() for tool in self._tools.values()]

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult.fail("NOT_FOUND", f"Инструмент {name!r} не зарегистрирован.")
        return tool.invoke(arguments)
