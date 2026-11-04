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

---

## M1: Domain schemas (2026-11-04)

**Built**: `src/adsmith/domain/` (pure, no I/O):
- `base.py`: `DomainModel` (`extra="forbid"`, whitespace stripping, assignment validation off on
  purpose), ULID ids with prefixes, canonical JSON (sorted, NFC, UTF-8) and namespaced `stable_hash`.
- `enums.py`: stages, scene status/phase, `StaleFrom` with depth ordering, cinematography
  vocabularies, failure kinds, recovery actions, languages/dialects, bible sections.
- `similarity.py`: bilingual trigram-Jaccard distinctness (ADR-0009).
- `brief.py`, `concept.py`, `scene.py`, `storyboard.py`, `continuity.py`, `generation.py`,
  `qa.py`, `edit.py`, `events.py`, `budget.py`, `run.py`.
- The scene sub-state machine is a declared `SCENE_TRANSITIONS` table plus atomic transition methods.

**Key decisions**
1. *Cross-object validation*: object-local rules live on the object (Storyboard owns a copy of
   `target_duration_s`); cross-aggregate rules live on `RunState` and run on every snapshot load.
   No hidden validation context (ADR-0010).
2. *Distinctness*: normalised word-bounded char-trigram Jaccard, threshold **0.35**, from measured
   classes (distinct ≤0.29, duplicates ≥0.39), pinned by calibration tests. The synonym-rewrite blind
   spot is documented and tested (ADR-0009).
3. *Hashing*: `content_hash` = creative spec only (`SPEC_FIELDS`); `keyframe_spec_hash` = spec
   minus motion fields; bible changes tracked per section; the generation cache key is separate
   (ADR-0010).
4. *Draft vs. domain models*: LLMs never produce IDs or lifecycle fields.
5. `QAVerdict.passed` is computed by code; `set_prompt` keeps the keyframe if and only if
   `keyframe_prompt` is unchanged.

**Deviations from plan**: see PLAN.md §14 "M1" (field renames, `shows_character/product`, dropped
`ScenePrompt.positive`). Ruff's ambiguous-Unicode rules (RUF001-003) are disabled because they flag
every Arabic letter.

**Tests**: `make check` green. **131 tests**: similarity (normalisation, calibration, margin, known
blind spot), scene FSM (happy path, every missing edge in the transition table, QA reject
keep/discard keyframe, stale depths, deepest-wins, invalid states on load), hashing (excluded
fields, every spec field, motion vs. look, NFC, namespace, Hypothesis property), invariants (brief,
concepts, storyboard, bible, patches, attempts, stage prerequisites, cross-aggregate rules, lossless
JSON round-trip).
A bug caught by the property test: the test factory derived `description` from `order`. Fixed in
the factory; the domain was correct.

**Next**: M2: storage (SQLite WAL, versioned snapshots with optimistic locking, event store with
tail, artifact/CAS store).
