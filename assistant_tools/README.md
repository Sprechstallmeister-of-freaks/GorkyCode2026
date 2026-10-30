# Assistant Tools Prototype

Независимый от LLM модуль инструментов для голосового ассистента.

## Что входит

- `create_task`, `create_reminder`, `create_meeting`, `list_events`, `search_events`.
- Единый `ToolRegistry`, provider-neutral tool definitions и единый формат результата.
- JSON Schema validation + семантическая проверка дат и временных интервалов.
- Лексический поиск через подключаемый адаптер `Retriever` (например, SQLite FTS5/BM25).
- Повторный поиск по альтернативным формулировкам, которые LLM генерирует только после нулевого результата первого поиска.
- Объединение результатов нескольких альтернатив через Reciprocal Rank Fusion (RRF).
- Абстракции `EventRepository`, `EventSearchService` и `Retriever`; поисковый модуль не зависит от конкретной LLM.

## Установка и тесты

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Подключение к приложению

```python
from assistant_tools import (
    CreateMeetingTool, CreateReminderTool, CreateTaskTool,
    ListEventsTool, SearchEventsTool, ToolRegistry,
)

registry = ToolRegistry([
    CreateTaskTool(event_repository),
    CreateReminderTool(event_repository),
    CreateMeetingTool(event_repository),
    ListEventsTool(event_repository),
    SearchEventsTool(search_service),
])

# Передать адаптеру выбранной LLM. Сами инструменты ничего не знают о формате API.
tool_definitions = registry.definitions()

# Передать registry.definitions() LLM-адаптеру.
# При получении function/tool call от LLM:
result = registry.execute("create_task", {
    "title": "Отправить отчёт",
    "description": None,
    "due_date": "2026-11-06",
    "due_at": None,
})
model_payload = result.to_dict()
```

## Контракты, которые должен реализовать слой хранения

`EventRepository`:

- `create_event(event: dict) -> dict`: сохраняет запись и возвращает её со стабильным `id`.
- `list_events(filters: dict) -> list[dict]`: возвращает не более `filters["limit"]` событий в полуоткрытом интервале `[start_at, end_at)`.
- Для `task` период определяется `due_at` или `due_date`; для `reminder` — `remind_at`; для `meeting` — `scheduled_at`.
- Задачи без срока не входят в выдачу за период.
- `created_at` хранится отдельно от даты самого события.

Для типов `task`, `reminder`, `meeting` формат полей создаваемой записи передаётся репозиторию в виде словаря с ключом `type` и полями соответствующего инструмента. Репозиторий может расширять возвращаемую запись системными полями (`id`, `created_at`, `status` и т. п.).

## Подключение лексического поиска с повторной формулировкой запроса

Реализовать адаптер `Retriever`, например на SQLite FTS5/BM25:

```python
from assistant_tools.search import LexicalEventSearch

lexical_retriever = SQLiteFts5Retriever(sqlite_connection)
search_service = LexicalEventSearch(lexical_retriever)
```

Передать `search_service` в `SearchEventsTool`. Сам `Retriever` должен применять фильтры `types`, `status`, `date_from` и `date_to` и возвращать `SearchHit` в порядке релевантности.

### Двухэтапный сценарий

1. Первый вызов `search_events`: LLM передаёт исходный `query`, не заполняя `query_variants`.
2. Если инструмент возвращает `count > 0`, поиск завершён.
3. Если инструмент возвращает `count == 0`, LLM формирует до пяти альтернативных формулировок и повторно вызывает `search_events`, передавая их в `query_variants`.
4. Сервис ищет каждую альтернативу лексически и объединяет результаты по RRF с удалением дубликатов.
5. Если повторный поиск тоже пустой, LLM сообщает, что записи не найдены. Повторять расширение бесконечно нельзя.

Пример второго вызова:

```json
{
  "query": "что я планировал для выступления",
  "query_variants": [
    "подготовка доклада",
    "презентация на конференции",
    "материалы для выступления"
  ],
  "types": [],
  "status": null,
  "date_from": null,
  "date_to": null,
  "limit": 10
}
```

`query_variants` намеренно не является обязательным полем: при первом поиске его не нужно передавать. LLM должна генерировать варианты только после получения пустого результата. Сам поисковый модуль не вызывает LLM и остаётся независимым от провайдера.

В системных инструкциях агенту также нужно зафиксировать порядок: сначала один вызов без `query_variants`; только при `count == 0` один повторный вызов с вариантами. На повторном вызове сохраняй исходные фильтры `types`, `status`, `date_from` и `date_to`. Если повторный поиск пустой, сообщай об отсутствии результатов и не запускай новые циклы расширения.

RRF здесь объединяет несколько лексических выдач, а не лексический и семантический поиск. Это эвристика ранжирования, а не семантическое понимание текста.

Важно: поисковый адаптер должен безопасно преобразовывать обычные пользовательские строки в синтаксис FTS5. Не передавайте произвольный текст LLM напрямую как выражение FTS5 без экранирования/построения запроса.

## Решения по контрактам

- `due_date` и `due_at` взаимоисключающие; оба `null` допустимы для задачи без срока.
- `remind_at` и `scheduled_at` обязательны и должны содержать часовой пояс.
- `end_at` встречи необязателен, но при наличии позже `scheduled_at`.
- Все nullable-поля явно включены в `required`; отсутствие поля не эквивалентно `null`.
- `types: []` означает все типы; `status: null` означает все статусы.
- `date_from`/`date_to` поиска фильтруют по дате события, не по `created_at`.
- `limit` ограничен диапазоном 1–100.
- `relevance` — относительная оценка ранжирования RRF, не вероятность совпадения.
- `query_variants` используется только при повторном вызове после нулевого результата первого поиска; максимум пять строк.
