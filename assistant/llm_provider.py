"""Drop-in contract for a model integrator. NO model SDK dependency here.

The integrator supplies a provider with interpret() and expand_search().
A provider NEVER executes tools or writes to SQLite; it only suggests commands.
"""
from __future__ import annotations

from typing import Protocol
from .contracts import LLMContext, ParsedCommand


class LLMProvider(Protocol):
    def interpret(self, text: str, context: LLMContext, tools: list[dict]) -> ParsedCommand:
        """Turn a free-form request into a validated tool name and JSON arguments."""
        ...

    def expand_search(self, query: str, context: LLMContext) -> list[str]:
        """Return up to five alternate search queries; never SQL."""
        ...


class NullLLMProvider:
    """Explicitly disabled implementation; all deterministic flows still work."""
    def interpret(self, text: str, context: LLMContext, tools: list[dict]) -> ParsedCommand:
        return ParsedCommand(tool="unknown")

    def expand_search(self, query: str, context: LLMContext) -> list[str]:
        return []
