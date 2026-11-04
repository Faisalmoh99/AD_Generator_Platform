"""Web API + single page (FastAPI). Same runtime and commands as the CLI; the page is static
HTML/CSS/JS in ``api/static`` and talks to these endpoints:

    GET  /api/config                  mode (scripted/mock …), budget, chaos default, examples
    GET  /api/runs                    recent runs
    POST /api/runs                    {request, auto, chaos} → starts the run in the background
    GET  /api/runs/{id}               the run's view model (api/view.py)
    GET  /api/runs/{id}/events        Server-Sent Events: the live agent trace (tails the store)
    POST /api/runs/{id}/choose        {concept}        (concept gate)
    POST /api/runs/{id}/approve                        (storyboard gate)
    POST /api/runs/{id}/edit          {text}           natural-language edit → blast radius
    POST /api/runs/{id}/retry         {scene}
    GET  /api/runs/{id}/pack.zip      Production Pack
    GET  /api/gallery                 curated real fal runs
    GET  /files/{path}                CAS media (keyframes, clips, finals), path-checked
    GET  /docs-media/{name}           committed real clips (fresh clone fallback)

Zero keys by default (scripted LLM + mock media). Work runs as background asyncio tasks; the
orchestrator's per-run lock serialises them, and every state change goes through the same
commands and repository as the CLI.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Coroutine
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from adsmith.agent import commands
from adsmith.agent.context import AgentRuntime
from adsmith.agent.edit.service import EditRejectedError, EditService
from adsmith.agent.export import zip_production_pack
from adsmith.agent.orchestrator import Orchestrator
from adsmith.api.gallery import load_gallery
from adsmith.api.view import event_json, run_view
from adsmith.config import Settings
from adsmith.media.endcard import FONT_DIR
from adsmith.storage import RunNotFoundError

STATIC = Path(__file__).parent / "static"
MEDIA_DIRS = ("cas", "runs")
MEDIA_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".webm", ".m4a"})
EXAMPLES = [
    "اعطني إعلان ١٥ ثانية لعطر رجالي فاخر يستهدف الشباب بأسلوب سينمائي",
    "Make me a 15-second ad for a luxury men's perfume targeting young men, cinematic.",
    "سوّ لي إعلان 10 ثواني لعطر رجالي فاخر للشباب، لقطة وحدة متواصلة، حركة بطيئة سينمائية",
]


class NewRun(BaseModel):
    request: str = Field(min_length=3, max_length=2000)
    auto: bool = False
    chaos: bool = False


class Choice(BaseModel):
    concept: str


class EditIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    resolution: str | None = None


class RetryIn(BaseModel):
    scene: int = Field(ge=1)


def ui_settings() -> Settings:
    s = Settings()
    if s.media_provider == "mock":
        # Mock media is priced at real Kling rates; give the demo enough *simulated* money for a
        # full run plus edits (real spend is still capped by the wallet ledger).
        s = s.model_copy(update={"budget_usd": max(s.budget_usd, 15.0)})
    return s


def create_app(settings: Settings | None = None) -> FastAPI:  # noqa: PLR0915 - route table
    s = settings or ui_settings()
    rt = AgentRuntime.from_settings(s)
    orch = Orchestrator(rt)
    tasks: dict[str, asyncio.Task[Any]] = {}
    app = FastAPI(title="Adsmith", docs_url="/api/docs")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.mount("/fonts", StaticFiles(directory=FONT_DIR), name="fonts")

    def background(run_id: str, work: Coroutine[Any, Any, Any]) -> None:
        old = tasks.get(run_id)
        if old is not None and not old.done():
            work.close()
            raise HTTPException(409, "the run is busy")
        tasks[run_id] = asyncio.create_task(work)

    def load(run_id: str) -> Any:
        try:
            return rt.storage.runs.load(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(404, "run not found") from exc

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))

    @app.get("/api/config")
    async def config() -> dict[str, Any]:
        return {
            "llm": s.llm_provider,
            "media": s.media_provider,
            "budget": s.budget_usd,
            "examples": EXAMPLES,
            "zero_key": s.llm_provider == "scripted" and s.media_provider == "mock",
        }

    @app.get("/api/runs")
    async def runs() -> list[dict[str, Any]]:
        return [
            {
                "id": r.id,
                "stage": r.stage.value,
                "request": r.request,
                "updated": r.updated_at.isoformat(),
            }
            for r in rt.storage.runs.list_runs(limit=30)
        ]

    @app.post("/api/runs")
    async def create(body: NewRun) -> dict[str, Any]:
        state = orch.create_run(body.request, interactive=not body.auto, chaos=body.chaos)
        background(state.id, orch.run_until_blocked(state.id))
        return {"id": state.id}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        view = run_view(load(run_id), rt.storage)
        task = tasks.get(run_id)
        view["busy"] = task is not None and not task.done()
        return view

    @app.post("/api/runs/{run_id}/choose")
    async def choose(run_id: str, body: Choice) -> dict[str, str]:
        try:
            commands.choose_concept(rt.storage, run_id, body.concept)
        except commands.CommandRejectedError as exc:
            raise HTTPException(409, str(exc)) from exc
        background(run_id, orch.run_until_blocked(run_id))
        return {"ok": "chosen"}

    @app.post("/api/runs/{run_id}/approve")
    async def approve(run_id: str) -> dict[str, str]:
        try:
            commands.approve_storyboard(rt.storage, run_id)
        except commands.CommandRejectedError as exc:
            raise HTTPException(409, str(exc)) from exc
        background(run_id, orch.run_until_blocked(run_id))
        return {"ok": "approved"}

    @app.post("/api/runs/{run_id}/edit")
    async def edit(run_id: str, body: EditIn) -> dict[str, Any]:
        try:
            outcome = await EditService(rt).edit(run_id, body.text, resolution=body.resolution)  # type: ignore[arg-type]
        except EditRejectedError as exc:
            raise HTTPException(409, str(exc)) from exc
        if outcome.status == "applied":
            background(run_id, orch.run_until_blocked(run_id))
        return {"status": outcome.status, "message": outcome.message}

    @app.post("/api/runs/{run_id}/retry")
    async def retry(run_id: str, body: RetryIn) -> dict[str, str]:
        try:
            commands.retry_scene(rt.storage, run_id, body.scene)
        except commands.CommandRejectedError as exc:
            raise HTTPException(409, str(exc)) from exc
        background(run_id, orch.run_until_blocked(run_id))
        return {"ok": "retrying"}

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, after: int = 0) -> StreamingResponse:
        load(run_id)

        async def stream() -> AsyncIterator[str]:
            seq = after
            idle = 0
            while True:
                batch = rt.storage.events.tail(run_id, after_seq=seq, limit=500)
                for e in batch:
                    seq = e.seq
                    yield f"id: {e.seq}\ndata: {json.dumps(event_json(e), ensure_ascii=False)}\n\n"
                idle = 0 if batch else idle + 1
                if idle and idle % 40 == 0:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.25)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/api/runs/{run_id}/pack.zip")
    async def pack(run_id: str) -> FileResponse:
        state = load(run_id)
        path = orch.export(state)
        if path is None:
            raise HTTPException(500, "export failed")
        archive = await asyncio.to_thread(zip_production_pack, path)
        return FileResponse(archive, filename=f"production_pack_{run_id}.zip")

    @app.get("/api/gallery")
    async def gallery() -> list[dict[str, Any]]:
        return load_gallery(s.config_dir, rt.storage)

    @app.get("/docs-media/{name}")
    async def docs_media(name: str) -> FileResponse:
        # Committed real clips (docs/media) for a fresh clone without data/: plain file names only.
        target = s.config_dir.parent / "docs" / "media" / name
        if "/" in name or ".." in name or target.suffix.lower() != ".mp4" or not target.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(target)

    @app.get("/files/{path:path}")
    async def files(path: str) -> FileResponse:
        # Media only: the database, scratch files and anything else in the data dir stay private.
        root = s.data_dir.resolve()
        target = (root / path).resolve()
        allowed = any((root / d) in target.parents for d in MEDIA_DIRS)
        if not allowed or target.suffix.lower() not in MEDIA_SUFFIXES or not target.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(target)

    return app
