"""Thin HTTP layer. Keep all rules in Planner and all tool contracts in ToolRegistry."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
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
from .models import ChatInput, ChatOutput, Event, EventCreate, EventList, EventPatch, Kind, Status, SearchResponse
from .routing import ChatRouter
from .service import Planner, NotFoundError, DomainError, ConflictError


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
    if cfg.import_seed and cfg.seed_csv:
        planner.import_csv(cfg.seed_csv)

    client = deepseek_client
    provider = llm_provider
    if provider is None:
        if client is None and cfg.deepseek_api_key:
            client = DeepSeekClient(api_key=cfg.deepseek_api_key, base_url=cfg.deepseek_base_url,
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
    app.state.planner = planner
    app.state.dialogue = dialogue
    app.state.tools = tools
    app.state.deepseek = client

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

    @app.get("/api/health", response_model=Health)
    def health():
        _, count = planner.list(limit=1, include_cancelled=True)
        model = cfg.deepseek_model if client is not None else "custom" if dialogue.llm_configured else None
        return Health(status="ok", llm_connected=dialogue.llm_connected,
                      llm_configured=dialogue.llm_configured, llm_model=model,
                      timezone=cfg.timezone, records=count)

    @app.post("/api/events", response_model=Event, status_code=201)
    def create_event(payload: EventCreate):
        return planner.create(payload)

    @app.get("/api/events", response_model=EventList)
    def list_events(from_date: date | None = None, to_date: date | None = None,
                    kind: Kind | None = None, include_cancelled: bool = False,
                    limit: Annotated[int, Query(ge=1, le=200)] = 100,
                    offset: Annotated[int, Query(ge=0)] = 0):
        rows, total = planner.list(from_date=from_date, to_date=to_date, kind=kind,
                                   include_cancelled=include_cancelled, limit=limit, offset=offset)
        return EventList(items=rows, total=total)

    @app.get("/api/search", response_model=SearchResponse)
    def search(q: Annotated[str, Query(min_length=2, max_length=500)], kind: Kind | None = None,
               limit: Annotated[int, Query(ge=1, le=100)] = 20):
        rows, total = planner.search(q, kind=kind, limit=limit)
        return SearchResponse(items=rows, total=total)

    @app.get("/api/events/{event_id}", response_model=Event)
    def get_event(event_id: int):
        return planner.get(event_id)

    @app.patch("/api/events/{event_id}", response_model=Event)
    def update_event(event_id: int, patch: EventPatch):
        if patch.status == Status.cancelled:
            raise HTTPException(status_code=409, detail="Для отмены требуется отдельное подтверждение: POST /api/events/{id}/cancel-request и затем POST /api/chat со словом 'да'")
        return planner.update(event_id, patch)

    @app.post("/api/events/{event_id}/cancel-request", response_model=ChatOutput,
              description="Start cancellation. It does NOT change data. User must confirm in a separate /api/chat request.")
    def request_cancel(event_id: int, payload: CancelRequest):
        event = planner.get(event_id)
        if event.status == Status.cancelled:
            raise HTTPException(status_code=409, detail="Событие уже отменено")
        state = planner.get_session(payload.session_id)
        planner.set_session(payload.session_id, active_event_id=state.get("active_event_id"),
            pending_action="confirm_cancel", pending_data={"tool": "cancel_event",
                "arguments": {"event_id": event.id, "expected_version": event.version}})
        return ChatOutput(answer=f"Подтверди отмену «{event.title}» (#{event.id}): да или нет?",
                          session_id=payload.session_id, action="clarify")

    @app.delete("/api/events/{event_id}", deprecated=True,
                description="Direct cancellation is disabled: separate user confirmation is required.")
    def cancel_event(event_id: int, expected_version: int | None = None):
        raise HTTPException(status_code=409,
            detail="Отмена без отдельного подтверждения запрещена. Сначала POST /api/events/{id}/cancel-request, затем POST /api/chat (да/нет).")

    @app.post("/api/chat", response_model=ChatOutput)
    def chat(payload: ChatInput):
        return dialogue.process(payload.message, payload.session_id)

    @app.get("/api/sessions/{session_id}/history")
    def history(session_id: str, limit: Annotated[int, Query(ge=1, le=200)] = 50):
        if len(session_id) > 80 or not __import__("re").fullmatch(r"[A-Za-z0-9_-]+", session_id):
            raise HTTPException(status_code=422, detail="Invalid session id")
        return {"messages": planner.history(session_id, limit=limit)}

    @app.get("/api/tools")
    def tool_specs():
        return {"tools": tools.specs(), "note": "Static schemas for LLM tool calling. Execution is internal, not a public arbitrary-tool endpoint."}

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(PROJECT_ROOT / "web" / "index.html", media_type="text/html")

    app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "web"), name="static")
    return app


app = create_app()
