# Progress log

One entry per milestone: what was built, key decisions, deviations from the plan, test results,
what's next. The plan is in [PLAN.md](PLAN.md); decisions are in [adr/](adr/).

---

## M0: Scaffold + docs (2026-11-04)

**Built**
- Toolchain: `uv` 0.13 (Homebrew), Python 3.12.16 pinned via `.python-version`, `pyproject.toml`
  (hatchling, src layout, `adsmith` console script), `Makefile` with the quality gate
  (`make check` = ruff check + ruff format --check + mypy --strict + pytest).
- `src/adsmith/config.py`:
  - `Settings` (pydantic-settings, `ADSMITH_` prefix, standard key names, nested per-node model
    overrides `ADSMITH_MODEL__<NODE>`).
  - `ModelCatalog` loaded from `config/models.toml` (profiles `fast|balanced|quality`, LLM prices,
    fal endpoints and prices). It validates that every profile covers every `LLMNode` and every
    model has a price.
  - `ChaosConfig` from `config/chaos.toml`.
- `src/adsmith/logging.py`: structlog (console/JSON) with context-var binding for `run_id`/`scene_id`.
- Package skeleton for every layer; CLI stub (`adsmith version`).
- Docs: `CLAUDE.md`, `docs/PLAN.md` (approved plan with revisions), ADRs 0001–0008, this log,
  README stub.

**Key decisions**
- Zero-key defaults: `llm_provider=scripted`, `media_provider=mock`, `qa_mode=auto` (→ mock critic
  with mock media). Real providers **fail fast at startup** when their key is missing instead of
  failing mid-run.
- OpenAI model IDs are left **empty** in `models.toml` on purpose. Using the OpenAI provider requires
  setting them from current docs (no IDs from memory).
- fal image prices are marked as estimates, to be verified in M11.

**Deviations from plan**
- `make check` (Makefile) instead of a task runner. Fewer dependencies; same gate.
- `docs/` and `prompts/` are excluded from ruff, because ruff was reformatting pseudo-code blocks in `PLAN.md`.

**Tests**: `make check` green: ruff clean, mypy --strict clean (22 files), 8/8 tests pass
(config defaults, profile completeness, per-node env override, quality profile, fail-fast on
missing key, Kling default, chaos config).

**Next**: M1: domain schemas (all Pydantic models, enums, invariants) with tests.
