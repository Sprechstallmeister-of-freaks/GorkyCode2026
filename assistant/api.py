"""Thin HTTP layer. Keep all rules in Planner and all tool contracts in ToolRegistry."""
from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings, from_env, PROJECT_ROOT
from .database import Database
from .deepseek import DeepSeekClient
from .deepseek_provider import DeepSeekProvider
from .llm_provider import LLMProvider
from .logging_config import (
    configure_logging,
    get_request_id,
    mask_sensitive,
    new_request_id,
    safe_body,
    set_request_id,
)
from .models import (
    ChatInput,
    ChatOutput,
    Event,
    EventCreate,
    EventList,
    EventPatch,
    Kind,
    NotificationItem,
    NotificationsDueResponse,
    Status,
    SearchResponse,
)
from .notifications import NotificationSettings, due_events
from .routing import ChatRouter
from .service import Planner, NotFoundError, DomainError, ConflictError

configure_logging()
import logging

logger = logging.getLogger("assistant.api")


class CancelRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")


class Health(BaseModel):
    status: str
    llm_connected: bool
    llm_configured: bool
    llm_model: str | None
    timezone: str
    records: int


def create_app(settings: Settings | None = None, *, llm_provider: LLMProvider | None = None,
               deepseek_client: DeepSeekClient | None = None) -> FastAPI:
    if llm_provider is not None and deepseek_client is not None:
        raise ValueError("Pass either llm_provider or deepseek_client, not both")
    cfg = settings or from_env()
    db = Database(cfg.db_path)
    planner = Planner(db, cfg.timezone)
    notification_settings = NotificationSettings(
        max_age_hours=cfg.notification_max_age_hours,
        tolerance_seconds=cfg.notification_tolerance_seconds,
    )
    if cfg.import_seed and cfg.seed_csv:
        planner.import_csv(cfg.seed_csv)

    client = deepseek_client
    provider = llm_provider
    if provider is None:
        if client is None and cfg.deepseek_keys:
            client = DeepSeekClient(api_keys=cfg.deepseek_keys, base_url=cfg.deepseek_base_url,
                                    model=cfg.deepseek_model, timeout_seconds=cfg.deepseek_timeout_seconds)
        if client is not None:
            provider = DeepSeekProvider(client)
    dialogue = ChatRouter(planner, provider=provider)
    tools = dialogue.tools

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if client is not None:
            client.close()

    app = FastAPI(title="Задачи и встречи — API", version="1.2.0",
                  description="DeepSeek tool calling, deterministic planner and rule fallback. Swagger: /docs",
                  lifespan=lifespan)
    app.state.db = db
    app.state.planner = planner
    app.state.dialogue = dialogue
    app.state.tools = tools
    app.state.deepseek = client
    app.state.notification_settings = notification_settings

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        rid = new_request_id()
        set_request_id(rid)
        start = time.perf_counter()

        # Тело читаем до вызова следующего handler-а (Starlette кэширует _body).
        body = await request.body() if request.method in {"POST", "PUT", "PATCH"} else None

        logger.info(
            "Входящий HTTP-запрос",
            extra={
                "method": request.method,
                "path": request.url.path,
                "query": mask_sensitive(str(request.query_params)) or None,
                "body": safe_body(body),
                "client_ip": request.client.host if request.client else None,
            },
        )

        try:
            response = await call_next(request)
        except Exception as exc:
            duration_ms = (time.perf_counter() - start) * 1000
            logger.exception(
                "Необработанная ошибка при обработке запроса",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": round(duration_ms, 2),
                    "error_type": type(exc).__name__,
                },
            )
            raise

        duration_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "Ответ HTTP-запроса",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round(duration_ms, 2),
            },
        )
        return response

    @app.exception_handler(NotFoundError)
    async def not_found(_request: Request, exc: NotFoundError):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(DomainError)
    async def domain_error(_request: Request, exc: DomainError):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def conflict_error(_request: Request, exc: ConflictError):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    def _health() -> Health:
        _, count = planner.list(limit=1, include_cancelled=True)
        model = cfg.deepseek_model if client is not None else "custom" if dialogue.llm_configured else None
        logger.debug("Проверка состояния сервиса", extra={"records": count, "llm_configured": dialogue.llm_configured})
        return Health(status="ok", llm_connected=dialogue.llm_connected,
                      llm_configured=dialogue.llm_configured, llm_model=model,
                      timezone=cfg.timezone, records=count)

    @app.get("/api/health", response_model=Health)
    def health():
        return _health()

    @app.get("/health", response_model=Health, include_in_schema=False)
    def health_alias():
        return _health()

    @app.get("/api/notifications/due", response_model=NotificationsDueResponse)
    def notifications_due():
        """События, время наступления которых наступило (опрос из браузера).

        Возвращает и атомарно помечает уведомлёнными, чтобы при повторном опросе
        то же уведомление не пришло дважды.
        """
        now_utc = datetime.now(timezone.utc)
        due = due_events(
            app.state.planner,
            app.state.db,
            now_utc=now_utc,
            settings=app.state.notification_settings,
        )
        logger.info(
            "Проверка уведомлений",
            extra={"action": "notifications_due", "count": len(due), "now_utc": now_utc.isoformat()},
        )
        items = [NotificationItem.model_validate(event.model_dump()) for event in due]
        return NotificationsDueResponse(items=items, count=len(items))

    @app.post("/api/events", response_model=Event, status_code=201)
    def create_event(payload: EventCreate):
        event = planner.create(payload)
        logger.info(
            "Событие создано через API",
            extra={"event_id": event.id, "action": "create_event", "kind": event.kind,
                   "title": event.title, "session_id": get_request_id()},
        )
        return event

    @app.get("/api/events", response_model=EventList)
    def list_events(from_date: date | None = None, to_date: date | None = None,
                    kind: Kind | None = None, include_cancelled: bool = False,
                    limit: Annotated[int, Query(ge=1, le=200)] = 100,
                    offset: Annotated[int, Query(ge=0)] = 0):
        rows, total = planner.list(from_date=from_date, to_date=to_date, kind=kind,
                                   include_cancelled=include_cancelled, limit=limit, offset=offset)
        logger.info(
            "Список событий через API",
            extra={"action": "list_events", "from_date": str(from_date) if from_date else None,
                   "to_date": str(to_date) if to_date else None, "kind": kind.value if kind else None,
                   "results": len(rows), "total": total, "session_id": get_request_id()},
        )
        return EventList(items=rows, total=total)

    @app.get("/api/search", response_model=SearchResponse)
    def search(q: Annotated[str, Query(min_length=2, max_length=500)], kind: Kind | None = None,
               limit: Annotated[int, Query(ge=1, le=100)] = 20):
        rows, total = planner.search(q, kind=kind, limit=limit)
        logger.info(
            "Поиск событий через API",
            extra={"action": "search", "query": q, "kind": kind.value if kind else None,
                   "results": len(rows), "total": total, "session_id": get_request_id()},
        )
        return SearchResponse(items=rows, total=total)

    @app.get("/api/events/{event_id}", response_model=Event)
    def get_event(event_id: int):
        event = planner.get(event_id)
        logger.info(
            "Событие получено через API",
            extra={"event_id": event_id, "action": "get_event", "session_id": get_request_id()},
        )
        return event

    @app.patch("/api/events/{event_id}", response_model=Event)
    def update_event(event_id: int, patch: EventPatch):
        if patch.status == Status.cancelled:
            logger.warning(
                "Попытка отмены через PATCH (запрещено)",
                extra={"event_id": event_id, "action": "update_event", "session_id": get_request_id()},
            )
            raise HTTPException(status_code=409, detail="Для отмены требуется отдельное подтверждение: POST /api/events/{id}/cancel-request и затем POST /api/chat со словом 'да'")
        event = planner.update(event_id, patch)
        logger.info(
            "Событие обновлено через API",
            extra={"event_id": event_id, "action": "update_event",
                   "fields": list(patch.model_dump(exclude_unset=True).keys()),
                   "session_id": get_request_id()},
        )
        return event

    @app.post("/api/events/{event_id}/cancel-request", response_model=ChatOutput,
              description="Start cancellation. It does NOT change data. User must confirm in a separate /api/chat request.")
    def request_cancel(event_id: int, payload: CancelRequest):
        event = planner.get(event_id)
        if event.status == Status.cancelled:
            logger.warning(
                "Запрос отмены уже отменённого события",
                extra={"event_id": event_id, "action": "cancel_request", "session_id": payload.session_id},
            )
            raise HTTPException(status_code=409, detail="Событие уже отменено")
        state = planner.get_session(payload.session_id)
        planner.set_session(payload.session_id, active_event_id=state.get("active_event_id"),
            pending_action="confirm_cancel", pending_data={"tool": "cancel_event",
                "arguments": {"event_id": event.id, "expected_version": event.version}})
        logger.info(
            "Запрос на отмену создан (ожидается подтверждение)",
            extra={"event_id": event_id, "action": "cancel_request", "session_id": payload.session_id},
        )
        return ChatOutput(answer=f"Подтверди отмену «{event.title}» (#{event.id}): да или нет?",
                          session_id=payload.session_id, action="clarify")

    @app.delete("/api/events/{event_id}", deprecated=True,
                description="Direct cancellation is disabled: separate user confirmation is required.")
    def cancel_event(event_id: int, expected_version: int | None = None):
        logger.warning(
            "Попытка прямого удаления через DELETE (запрещено)",
            extra={"event_id": event_id, "action": "cancel_event", "session_id": get_request_id()},
        )
        raise HTTPException(status_code=409,
            detail="Отмена без отдельного подтверждения запрещена. Сначала POST /api/events/{id}/cancel-request, затем POST /api/chat (да/нет).")

    @app.post("/api/chat", response_model=ChatOutput)
    def chat(payload: ChatInput):
        logger.info(
            "Входящий чат-запрос",
            extra={"session_id": payload.session_id, "action": "chat",
                   "message_len": len(payload.message), "message_preview": payload.message[:120]},
        )
        result = dialogue.process(payload.message, payload.session_id)
        # Человекочитаемая переписка в chat_log.txt (путь: ASSISTANT_CHAT_LOG).
        logging.getLogger("assistant.chat_transcript").info(
            "session=%s\nПОЛЬЗОВАТЕЛЬ: %s\nАССИСТЕНТ [action=%s mode=%s]: %s\n",
            payload.session_id, payload.message, result.action, result.mode, result.answer,
        )
        logger.info(
            "Чат-запрос обработан",
            extra={"session_id": payload.session_id, "action": result.action,
                   "mode": result.mode, "needs_llm": result.needs_llm,
                   "has_event": result.event is not None,
                   "events_count": len(result.events),
                   "answer_preview": result.answer[:120]},
        )
        return result

    @app.get("/api/sessions/{session_id}/history")
    def history(session_id: str, limit: Annotated[int, Query(ge=1, le=200)] = 50):
        if len(session_id) > 80 or not __import__("re").fullmatch(r"[A-Za-z0-9_-]+", session_id):
            raise HTTPException(status_code=422, detail="Invalid session id")
        messages = planner.history(session_id, limit=limit)
        logger.info(
            "История сессии запрошена",
            extra={"session_id": session_id, "action": "history", "messages_count": len(messages)},
        )
        return {"messages": messages}

    @app.get("/api/tools")
    def tool_specs():
        return {"tools": tools.specs(), "note": "Static schemas for LLM tool calling. Execution is internal, not a public arbitrary-tool endpoint."}

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(PROJECT_ROOT / "web" / "index.html", media_type="text/html")

    app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "web"), name="static")
    return app


app = create_app()
