"""Thin HTTP layer. Keep all rules in Planner and all tool contracts in ToolRegistry."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import Settings, from_env, PROJECT_ROOT
from .database import Database
from .dialogue import RuleDialogue
from .models import ChatInput, ChatOutput, Event, EventCreate, EventList, EventPatch, Kind, SearchResponse
from .service import Planner, NotFoundError, DomainError, ConflictError
from .tools import ToolRegistry


class Health(BaseModel):
    status: str
    llm_connected: bool
    timezone: str
    records: int


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or from_env()
    db = Database(cfg.db_path)
    planner = Planner(db, cfg.timezone)
    if cfg.import_seed and cfg.seed_csv:
        planner.import_csv(cfg.seed_csv)
    dialogue = RuleDialogue(planner)
    tools = ToolRegistry(planner)
    app = FastAPI(title="Задачи и встречи — API", version="1.0.0",
                  description="LLM-independent planner. Swagger: /docs")
    app.state.planner = planner
    app.state.dialogue = dialogue
    app.state.tools = tools

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
        return Health(status="ok", llm_connected=False, timezone=cfg.timezone, records=count)

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
        return planner.update(event_id, patch)

    @app.delete("/api/events/{event_id}", response_model=Event,
                description="Soft delete: moves status to cancelled; records are not physically erased.")
    def cancel_event(event_id: int, expected_version: int | None = None):
        return planner.cancel(event_id, expected_version=expected_version)

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
        return {"tools": tools.specs(), "note": "Schemas for a future LLM. Execution is internal, not an unauthenticated public endpoint."}

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(PROJECT_ROOT / "web" / "index.html", media_type="text/html")

    app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "web"), name="static")
    return app


app = create_app()
