"""DeepSeek implementation of the model-neutral LLMProvider boundary."""
from __future__ import annotations

import json

from .contracts import LLMContext, ParsedCommand
from .deepseek import DeepSeekClient, DeepSeekError
from .tools import DEFINITIONS


class DeepSeekProvider:
    """Translate provider tool calls to ParsedCommand without executing them."""

    def __init__(self, client: DeepSeekClient):
        self.client = client

    @property
    def connected(self) -> bool:
        return self.client.connected

    def interpret(self, text: str, context: LLMContext, tools: list[dict]) -> ParsedCommand:
        prompt = f"""Ты — русскоязычный ассистент задач, напоминаний и встреч.
Текущие дата и время: {context.now.isoformat()}; часовой пояс: {context.timezone}.
Выбери не более одного инструмента и передай JSON-аргументы строго по его схеме. Не выполняй операцию сам и никогда не создавай SQL, Python или shell.
Преобразуй относительные даты в ISO относительно времени выше. Если у встречи или напоминания нет даты/времени, всё равно выбери create_event с известными полями: сервер задаст уточняющий вопрос.
Не выдумывай event_id. Если запись названа, но её ID неизвестен, для get_event, update_event или cancel_event добавь в аргументы строку target_query с поисковым текстом; адаптер вынесет её до валидации инструмента. Для местоимения «её/его» без названия оставь и event_id, и target_query пустыми — сервер использует активный контекст.
Если свободный запрос не относится к задачам и встречам, не вызывай инструмент."""
        message = self.client.complete([
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ], tools)
        calls = message.get("tool_calls") or []
        if not calls:
            return ParsedCommand(tool="unknown")
        if len(calls) != 1:
            raise DeepSeekError("Модель запросила несколько параллельных инструментов")
        name, args = self._parse_call(calls[0])
        if name not in DEFINITIONS:
            raise DeepSeekError(f"Неизвестный инструмент: {name}")
        target_query = args.pop("target_query", None)
        return ParsedCommand(tool=name, arguments=args, target_query=target_query)

    def expand_search(self, query: str, context: LLMContext) -> list[str]:
        prompt = f"""Предложи до пяти коротких альтернативных формулировок для поиска личной задачи или встречи.
Сохрани смысл, используй возможные синонимы и словоформы, не выдумывай новые факты. Верни ТОЛЬКО JSON-массив строк без markdown и пояснений. Не создавай SQL.
Текущее время: {context.now.isoformat()}; часовой пояс: {context.timezone}."""
        message = self.client.complete([
            {"role": "system", "content": prompt},
            {"role": "user", "content": query},
        ], [])
        try:
            variants = json.loads(self._json_content(message.get("content")))
        except (json.JSONDecodeError, TypeError) as exc:
            raise DeepSeekError("Модель вернула некорректные варианты поиска") from exc
        if not isinstance(variants, list):
            raise DeepSeekError("Варианты поиска должны быть JSON-массивом")
        result = []
        for item in variants:
            if isinstance(item, str) and 2 <= len(item.strip()) <= 500 and item.strip() not in result:
                result.append(item.strip())
        return result[:5]

    def describe(self, text: str, result: dict, context: LLMContext) -> str:
        """Optionally phrase actual read-only results; never used for mutations."""
        prompt = """Кратко и естественно ответь по-русски на запрос пользователя строго по JSON-результату инструмента.
Не выдумывай записи, даты, статусы или ID. Сохрани фактические даты и время. Не упоминай SQL, JSON и внутреннюю реализацию. Не выполняй новых действий."""
        message = self.client.complete([
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(
                {"request": text, "tool_result": result}, ensure_ascii=False)},
        ], [])
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise DeepSeekError("Пустое описание результата инструмента")
        return content.strip()

    @staticmethod
    def _parse_call(call: dict) -> tuple[str, dict]:
        if not isinstance(call, dict):
            raise DeepSeekError("Некорректный tool call")
        function = call.get("function")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            raise DeepSeekError("Некорректный function в tool call")
        raw_args = function.get("arguments")
        if not isinstance(raw_args, str):
            raise DeepSeekError("Аргументы инструмента должны быть JSON-строкой")
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError as exc:
            raise DeepSeekError("Инструмент получил malformed JSON") from exc
        if not isinstance(args, dict):
            raise DeepSeekError("Аргументы инструмента должны быть JSON-объектом")
        return function["name"], args

    @staticmethod
    def _json_content(content: str | None) -> str:
        if not isinstance(content, str):
            raise TypeError("JSON content must be a string")
        stripped = content.strip()
        if stripped.startswith("```") and stripped.endswith("```"):
            lines = stripped.splitlines()
            stripped = "\n".join(lines[1:-1]).strip()
        return stripped

    def close(self):
        self.client.close()
