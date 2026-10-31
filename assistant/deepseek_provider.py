"""DeepSeek implementation of the model-neutral LLMProvider boundary."""
from __future__ import annotations

import json
import logging
import time

from .contracts import LLMContext, ParsedCommand
from .deepseek import DeepSeekClient, DeepSeekError
from .logging_config import mask_sensitive
from .tools import DEFINITIONS

logger = logging.getLogger("assistant.llm")


class DeepSeekProvider:
    """Translate provider tool calls to ParsedCommand without executing them."""

    def __init__(self, client: DeepSeekClient):
        self.client = client

    @property
    def connected(self) -> bool:
        return self.client.connected

    def interpret(self, text: str, context: LLMContext, tools: list[dict]) -> ParsedCommand:
        events_section = ""
        if context.events:
            events_lines = []
            for ev in context.events[:30]:
                events_lines.append(
                    f"  - #{ev.get('id')} {ev.get('title')} [{ev.get('kind')}] "
                    f"{ev.get('scheduled_date') or 'без даты'}"
                    f"{' в ' + ev.get('scheduled_time') if ev.get('scheduled_time') else ''} "
                    f"статус={ev.get('status')}"
                )
            events_section = "\nСобытия из календаря пользователя (используй для ответов на вопросы о расписании):\n" + "\n".join(events_lines)

        prompt = f"""Ты — русскоязычный ассистент задач, напоминаний и встреч.
Текущие дата и время: {context.now.isoformat()}; часовой пояс: {context.timezone}.{events_section}
Выбери не более одного инструмента и передай JSON-аргументы строго по его схеме. Не выполняй операцию сам и никогда не создавай SQL, Python или shell.
Преобразуй относительные даты в ISO относительно времени выше. Если у встречи или напоминания нет даты/времени, всё равно выбери create_event с известными полями: сервер задаст уточняющий вопрос.
Сначала определи намерение. Вопросы «что нужно сделать», «сколько дел», «что запланировано» и «есть ли что-то после» — это чтение календаря, а не просьба создать запись. Для вопроса о делах на день выбери list_events и укажи одинаковые from_date и to_date. Не задавай kind, если тип записи явно не запрошен.
Выбирай create_event только при явном намерении добавить, запланировать или напомнить. Желание начать что-то делать, условная конструкция и вопрос о свободном времени сами по себе не являются командой создания.
Не выдумывай event_id. Если запись названа, но её ID неизвестен, для get_event, update_event или cancel_event добавь в аргументы строку target_query с поисковым текстом; адаптер вынесет её до валидации инструмента. Для местоимения «её/его» без названия оставь и event_id, и target_query пустыми — сервер использует активный контекст.
Если свободный запрос не относится к задачам и встречам, не вызывай инструмент."""
        prompt_chars = len(prompt) + len(text)
        tool_names = [t.get("function", {}).get("name", "?") for t in tools]
        start = time.perf_counter()
        logger.info(
            "Запрос к LLM (interpret)",
            extra={"action": "llm_call",
                   "llm_method": "interpret", "llm_prompt_chars": prompt_chars,
                   "llm_tools": tool_names, "user_text_preview": text[:120]},
        )
        message = self.client.complete([
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ], tools)
        latency_ms = (time.perf_counter() - start) * 1000
        calls = message.get("tool_calls") or []
        logger.info(
            "Ответ от LLM (interpret)",
            extra={"action": "llm_call",
                   "llm_method": "interpret", "llm_latency_ms": round(latency_ms, 2),
                   "llm_tool_calls": len(calls),
                   "llm_tool_names": [c.get("function", {}).get("name") for c in calls if isinstance(c, dict)],
                   "llm_response_keys": list(message.keys())},
        )
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
        prompt_chars = len(prompt) + len(query)
        start = time.perf_counter()
        logger.info(
            "Запрос к LLM (expand_search)",
            extra={"action": "llm_call",
                   "llm_method": "expand_search", "llm_prompt_chars": prompt_chars,
                   "query_preview": query[:120]},
        )
        message = self.client.complete([
            {"role": "system", "content": prompt},
            {"role": "user", "content": query},
        ], [])
        latency_ms = (time.perf_counter() - start) * 1000
        content = message.get("content")
        try:
            variants = json.loads(self._json_content(content))
        except (json.JSONDecodeError, TypeError) as exc:
            logger.exception("Ошибка разбора ответа LLM (expand_search)",
                             extra={"action": "llm_call",
                                    "llm_method": "expand_search", "llm_latency_ms": round(latency_ms, 2),
                                    "llm_response_preview": mask_sensitive(str(content))[:200] if content else None,
                                    "error_type": type(exc).__name__})
            raise DeepSeekError("Модель вернула некорректные варианты поиска") from exc
        if not isinstance(variants, list):
            logger.warning("LLM вернул не список вариантов поиска",
                           extra={"action": "llm_call",
                                  "llm_method": "expand_search", "llm_latency_ms": round(latency_ms, 2),
                                  "llm_response_type": type(variants).__name__})
            raise DeepSeekError("Варианты поиска должны быть JSON-массивом")
        result = []
        for item in variants:
            if isinstance(item, str) and 2 <= len(item.strip()) <= 500 and item.strip() not in result:
                result.append(item.strip())
        logger.info(
            "Ответ от LLM (expand_search)",
            extra={"action": "llm_call",
                   "llm_method": "expand_search", "llm_latency_ms": round(latency_ms, 2),
                   "variants": result, "variants_count": len(result)},
        )
        return result[:5]

    def describe(self, text: str, result: dict, context: LLMContext) -> str:
        """Optionally phrase actual read-only results; never used for mutations."""
        prompt = """Кратко и естественно ответь по-русски на запрос пользователя строго по JSON-результату инструмента.
Не выдумывай записи, даты, статусы или ID. Сохрани фактические даты и время. Не упоминай SQL, JSON и внутреннюю реализацию. Не выполняй новых действий."""
        prompt_chars = len(prompt) + len(text) + len(str(result))
        start = time.perf_counter()
        logger.info(
            "Запрос к LLM (describe)",
            extra={"action": "llm_call",
                   "llm_method": "describe", "llm_prompt_chars": prompt_chars,
                   "user_text_preview": text[:120]},
        )
        message = self.client.complete([
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(
                {"request": text, "tool_result": result}, ensure_ascii=False)},
        ], [])
        latency_ms = (time.perf_counter() - start) * 1000
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            logger.warning("Пустое описание от LLM (describe)",
                           extra={"action": "llm_call",
                                  "llm_method": "describe", "llm_latency_ms": round(latency_ms, 2),
                                  "error_type": "empty_response"})
            raise DeepSeekError("Пустое описание результата инструмента")
        logger.info(
            "Ответ от LLM (describe)",
            extra={"action": "llm_call",
                   "llm_method": "describe", "llm_latency_ms": round(latency_ms, 2),
                   "answer_preview": content[:120]},
        )
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
