# CLAUDE.md

**Adsmith** is a marketing-ad agent. It turns an ad request (English, MSA or Saudi dialect) into 3
concepts → a storyboard with shot specs → video-generation prompts → keyframes → clips → a stitched
ad. It recovers from failures and applies targeted scene edits without redoing the job.

- Plan and architecture: `docs/PLAN.md`
- Decisions (why things are the way they are): `docs/adr/`
- What has been built so far: `docs/PROGRESS.md`

## Commands

```bash
make setup                         # uv sync (Python 3.12 via uv)
uv run adsmith --help              # CLI
uv run adsmith run "<request>"     # start a run (zero-key by default: scripted LLM + mock media)
uv run adsmith serve               # web UI
make test                          # uv run pytest
make lint                          # ruff check + ruff format --check
make typecheck                     # mypy --strict src tests
make check                         # FULL QUALITY GATE: lint + typecheck + test
uv run pytest --live -m live       # opt-in real-API smoke tests (needs keys)
```

## Architecture rules (non-negotiable)

1. **The LLM never decides control flow.** Stages and transitions are a declared table in
   `agent/orchestrator.py`; `next_stage(state)` is a pure function. LLMs reason inside nodes and
   only fill schema fields.
2. **All state changes go through typed deltas and the orchestrator.** Nodes return deltas; they do
   not mutate persisted state or touch the DB.
3. **Scenes never write state directly.** Scene workers return results; a single writer applies them
   and persists a snapshot.
4. **No model IDs, endpoints or prices outside `config/`.** Code asks `Settings.node_model(node)`
   or the catalog.
5. **Prompts live only in `prompts/<node>/vN.md`** as versioned Jinja2 templates with frontmatter.
   No inline prompt strings in Python. Change a prompt → bump its version.
6. **Provider SDK retries are disabled** (`max_retries=0`). The recovery layer
   (`agent/recovery/`) owns every retry so each one is classified, bounded, budgeted and traced.
7. Every LLM call returns a **validated Pydantic object** (or raises a typed `AgentError`).
   Invalid output goes through the schema-repair loop and never crashes the run.
8. One scene failing must never block or invalidate another scene.
9. User-facing text follows the request language; generation prompts are always English.

## Code standards

- Python 3.12, full type hints, `mypy --strict` clean, `ruff` clean (config in `pyproject.toml`).
- Pydantic v2 models with `extra="forbid"`; bounded fields (`Field(min_length/max_length/ge/le)`).
- No bare `except`, and no `except Exception` without re-raising or converting to a typed error.
- Async I/O for providers; inject clock, sleep and RNG so recovery is deterministic in tests.
- Every recovery path has a test. LLMs are mocked (`ScriptedLLMProvider`) in unit tests.
- Arabic text in source, fixtures and prompts is stored in **logical order** (never "fixed" to
  look right in a terminal).

## Layout

```
src/adsmith/domain/     Pydantic schemas (state)          agent/       orchestrator, nodes, recovery, edit
src/adsmith/providers/  LLM / image / video + adapters    storage/     SQLite snapshots, events, cache
src/adsmith/api/        FastAPI + SSE + single page       cli/         Typer + Rich
prompts/                versioned templates               config/      models.toml, chaos.toml
```

## Workflow

- Run `make check` before every commit; never commit red.
- Conventional commits (`feat(agent): …`, `fix(storage): …`, `test: …`, `docs: …`, `chore: …`),
  small and per milestone.
- At the end of each milestone: update `docs/PROGRESS.md` (built / decisions / deviations / test
  results / next), then stop for review.
- Any architectural change or significant decision → new ADR in `docs/adr/NNNN-title.md`
  (Context / Decision / Alternatives considered / Consequences), and update `docs/PLAN.md` if the
  plan changed.
