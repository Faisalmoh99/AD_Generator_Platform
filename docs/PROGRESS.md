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

---

## Pre-M2 fix: fail-safe scene flags (2026-11-04)
`shows_character` / `shows_product` default to `True`. `domain/scene_flags.py` upgrades (never
downgrades) a flag when the scene text mentions the character or product, using generic EN/AR terms
and, once the bible exists, wardrobe and product-name terms. Each override becomes a `decision`
event. The asymmetry is documented in ADR-0010. 8 tests. Wiring into the storyboard/continuity
nodes and edits is listed as an M4/M7 must in PLAN.md §14.

**Process slip, corrected:** the first commit of this fix ran after a red typecheck, because
`make check | tail -1` hid the exit code. The fix was amended before moving on (local, unpushed). The
gate is now always run to a log file and its exit code checked before any commit.

---

## M2: Storage (2026-11-04)

**Built** (`src/adsmith/storage/`)
- `db.py`: SQLite (WAL, `synchronous=NORMAL`, FKs, 5 s busy timeout), one locked connection per
  process, explicit `write()` transactions (`BEGIN IMMEDIATE`, all-or-nothing), append-only
  migrations tracked with `PRAGMA user_version`.
- `repo.py`: `RunRepository`: `create`, `update(mutate_fn)` (atomic read-modify-write plus events),
  `load`, `load_version`, `history`, `list_runs`, `fork`. Errors: `RunNotFoundError`,
  `ConcurrencyConflictError` (optimistic lock, opt-in), `StaleWriteError` (mutation veto).
- `events.py`: `EventStore` with gap-free per-run `seq` (assigned in-transaction), `tail(after_seq)`.
- `artifacts.py`: content-addressed store with atomic writes and path-escape protection.
- `cache.py`: request-hash → artifact cache; first writer wins; self-healing on missing files.
- `Storage.open(data_dir)` facade.

**Key decisions** (ADR-0011)
1. Snapshot growth: full compressed JSON per version, ~3–4 KB each, ~0.1–0.3 MB per run. Smaller
   than the run's video, and it buys trivial resume and time travel.
2. Event `seq` = `MAX+1` inside the serialised write transaction: monotonic, gap-free, commit order
   = seq order.
3. Conflicts: automated writers can't lose updates (the read happens under the write lock).
   Outdated scene results are vetoed semantically. `expected_version` is only for human commands.

**Tests**: `make check` green, **160 tests** (+21 storage): round-trips, history/time travel,
rollback on mutation error and on invalid resulting state, no seq consumed on rollback, optimistic
lock, **2 connections × 4 threads × 25 writes** (no lost updates, versions contiguous, seqs
gap-free), concurrent asyncio appends, stale-result veto, tail semantics, Arabic payload
round-trip, fork, compressed snapshot < 8 KB, CAS idempotency/move/path escape, cache semantics.

**Next**: M3: LLM layer (`LLMProvider`, Anthropic + Scripted + OpenAI skeleton, `structured_call`
with the schema-repair loop, prompt-template loader).

---

## Pre-M3 check: write lock (2026-11-04)
Confirmed `Database.write()` uses `BEGIN IMMEDIATE`, and `busy_timeout=5000` is set both as a pragma
and as the connect timeout. Added `test_storage_locking.py`: two connections, where the second writer must
block until the first commits. A **control test** with a deferred-`BEGIN` subclass shows the detector
does catch the race, so the test is sensitive and not vacuous. Pragmas are asserted (busy_timeout,
WAL, FKs).

---

## M3: LLM layer (2026-11-04)

**Built**
- `providers/llm/`: `base.py` (`LLMProvider.complete`, messages with cache flags and native-block
  replay), `anthropic.py` (beta `messages.create`, `output_config` effort+format, cache breakpoints,
  server-side fallback, refusal/usage mapping), `openai.py` (Responses API, strict json_schema;
  unit-tested mapping, not run live), `scripted.py` (steps: output/raw/refusal/truncated/error) +
  packaged `fixtures/perfume_en/` (understand, ideate, storyboard, continuity).
- `agent/structured.py`: `StructuredCaller` (validate + convert, same-conversation repair ×2,
  truncation retry ×1, refusal → `ContentPolicyError`, transient backoff, per-attempt
  `LLMCallRecord`).
- `agent/recovery/`: error taxonomy, full-jitter backoff with separate transient/429 budgets,
  SDK exception classifier (Anthropic + OpenAI).
- `agent/prompts.py` (versioned templates, static SYSTEM enforced), `prompts/schema_repair/v1.md`,
  `agent/schemas.py` registry, `agent/pricing.py`, `domain/llm.py`, `BudgetLedger.record_actual`.

**Key decisions** (ADR-0012)
1. Verified on the installed SDK: anthropic **2.4.0** (on `httpx2` 2.16.0). `messages.parse` and
   `output_config` exist, but `parse()` validates eagerly and loses raw text and usage on failure, so we
   use `create` + our own validation. `transform_schema` is public and moves unenforced constraints
   into descriptions.
2. Repair is appended to the same conversation (native blocks replayed unchanged).
3. Prompt caching on the static SYSTEM block and a per-run CONTEXT block (~70% off repeated
   prefixes; cents per run, mainly a latency win).
4. Scripted fixtures are validated against the registry schemas and through the real domain
   conversions, with an Arabic logical-order check (no presentation forms, no bidi controls).
5. Cost per call from token buckets → `LLMCallRecord` → `BudgetLedger` + trace event. Failed
   calls are booked too.

**Bugs caught by the new tests**
- Scene-flag checker treated "hero" as a character, but in ad language a *hero shot* is the product
  shot. Term removed, regression test added.
- Ruff's PLE2502 caught literal bidi control characters in a test regex; rewritten as ASCII
  escapes.
- mypy caught that anthropic 2.x uses `httpx2`; tests now build `httpx2` objects.

**Deviations from plan**: see PLAN.md §14 "M3". Recovery primitives were pulled forward from M5.

**Tests**: `make check` green, **219 passed, 2 skipped**. The 2 skipped are the live tests
(`--live`): **not run**, because there is no `ANTHROPIC_API_KEY` on this machine. They check that
configured model IDs exist via the Models API, and that a structured call is priced and its
repeated prefix is read from cache.

**Next**: M4: orchestrator + planning nodes (understand → ideate → select → storyboard →
continuity → prompt), human gates, resume, scene-flag wiring, Production Pack, Arabic fixtures.
