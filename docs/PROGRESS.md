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

---

## Live API verification (2026-11-04)
`uv run pytest --live -m live`: **2 passed**. (The tests now read the key via `Settings`, i.e. env
or `.env`, like the app does; before, they only looked at `os.environ`.)
- Models API: `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5` all exist.
- Structured output through our adapter works on Haiku 5.5 and Sonnet 5.5.
- Prompt caching confirmed: Sonnet call 1 wrote 2,358 prefix tokens ($0.0063); call 2 read them
  ($0.00086, ~83% cheaper than uncached). Haiku hit the cache written by an earlier run.
- No refusals occurred, so served model = requested model.

---

## M4a: Orchestrator + understand / ideate / select (2026-11-04)

**Built**
- `agent/transitions.py`: declared table of pure guards for every loop stage (interactive vs. auto,
  QA loop, done vs. done_partial); `GATE_EXITS`.
- `agent/deltas.py`: typed deltas + `apply_delta`; `agent/nodes/{base,common,understand,ideate,
  select}.py`; `agent/context.py` (`AgentRuntime`, `NodeContext` without storage).
- `agent/orchestrator.py`: `create_run`, `run_until_blocked(stop_before)`, `step` (atomic commit
  of deltas + LLM cost + events + next stage, timing per stage), `resume` (errored → failed stage,
  lease release), failure → `errored` with user-language reason, stale result → discarded + event.
- `agent/commands.py`: `choose_concept` (number or id, stage check, optional expected_version).
- `agent/i18n.py`: EN/AR stage labels and failure messages.
- Prompts `understand/v1`, `ideate/v1`, `select/v1`; judge schema `ConceptJudgementDraft` (LLM
  scores 1-10 on 5 weighted criteria, code picks the argmax); `select` fixture; deterministic
  `detect_language`.
- CLI: `run [--auto] [--until STAGE]`, `resume`, `choose`, `show`, `trace`.

**Key decisions** (ADR-0013): typed deltas; nodes can't touch storage; failures are run state, not
crashes; resume is always safe; language from brief, else request script.

**Real-API check of the new prompts** (one Arabic request, `--auto`, balanced profile):
understand 19.7 s / $0.0007 (Haiku), ideate 41.6 s / $0.052 (Sonnet high), select 6.4 s / $0.012,
0 repairs. It surfaced two prompt bugs, fixed before commit:
1. Concept 3 copied the illustrative example in the ideate SYSTEM prompt (a reflection that smiles a
   second early). Replaced with an example from an unrelated category, plus "never reuse examples
   from these instructions". The rerun produced no borrowed imagery.
2. The select rationale was in English for an Arabic request. The select prompt now takes the user
   language, and the rerun rationale is in Arabic.
Also found: cache writes are ~2-2.6k tokens per call against ~600-token system prompts, which
suggests the output JSON schema is part of the cached prefix. Haiku `understand` at medium effort is
slow (20 s) and will be tuned in M4b.

**Bug found by tests**: ledger rounded to 6 dp while call records use 8 dp, so the ledger drifted
from the trace. Unified on `USD_DECIMALS = 8`.

**Tests**: `make check` green, **254 passed, 2 skipped** (live). New: transitions (every edge,
gates/terminals illegal, table completeness), orchestrator (event sequence, snapshot versions,
cost = trace, choose + version conflict, auto-select, stop_before, language override, typed
failure → resume, failed calls booked, Arabic error before brief, unexpected exception, stale
result discarded, resume in a fresh process with no re-calls).

**Next**: M4b: storyboard → continuity → prompt, scene-flag wiring, Production Pack, Arabic
fixtures, then the 3 real runs and critique.

---

## M4b: Storyboard → continuity → prompt, Production Pack, real runs (2026-11-04)

**Built**
- Nodes + prompts: `storyboard` (DP-level shot specs, video-model duration floor, 9:16 composition
  with a tagline safe zone), `continuity` (bible + upgrade-only flag check with bible terms),
  `prompt` (keyframe/motion/negative per scene; cache-warming order; per-scene failure isolation).
- `agent/export.py`: Production Pack (`storyboard.json` + bilingual `storyboard.md`), post-commit
  hook after `prompt`, CLI `adsmith export`.
- Creative guardrails (ADR-0014): cliché list first in the ideate schema; judge originality plus
  a cliché penalty.
- Latency tuning (ADR-0007): understand low = 3.5 s median (was 5.7 s at medium). ideate kept at
  high: 49.6 s vs 21.2 s at medium, but medium/low regressed to category clichés.
- `scripts/harvest_fixtures.py`; `perfume_ar` fixtures harvested from a real run.

**Real runs** (balanced, `--auto`, stop before keyframe), first pass → after fixes:

| Run | Total time | LLM cost | Repairs | Flag overrides |
|---|---|---|---|---|
| AR perfume (15 s) | 106 s → 97 s | $0.176 → $0.181 | 0 → 0 | 2 (FP) → 0 |
| EN Jeddah coffee (15 s) | 107 s → 98 s | $0.217 → $0.185 | 2 → 0 | 1 (FP) → 0 |
| AR delivery app (10 s) | 114 s → 103 s | $0.207 → $0.157 | 2 → 0 | 1 (FP) → 0 |

Per stage (typical): understand ~4 s / $0.0005 · ideate ~40 s / $0.05 · select ~6 s / $0.013 ·
storyboard ~22 s / $0.03 · continuity ~14 s / $0.025 · prompt ~12 s / $0.05 (4 parallel calls).

**Defects found only by reading real outputs (fixed, with regression tests):**
perfume-shaped `ProductSpec`; "women" in forbidden/negative prompts (cultural over-correction);
arbitrary length limits causing repairs; noisy bible-term flag detection; ungrammatical bible
`prompt_block` ("cap cap"); judge rationale language; prompt-example copying (M4a).

**Open quality issues (not fixed, carried forward):** multi-action shots in energetic briefs;
negative prompts that contradict the positive (e.g. "additional people" alongside a courier);
keyframe prompts ~230–260 words against a 60–120 target; the scale-trick "reveal" concept is not
physically achievable as one continuous shot; dialect `other` for a clearly Gulf request at low
effort; prompt-injection and invalidation share the scene flags (ADR-0010 addendum).

**Tests**: `make check` green, **281 passed, 2 skipped** (live). New: judge (weights, penalty
flips the winner, tie-breaks), planning (full pipeline + pack, cache-warming order, per-scene
failure isolation, duration floor repair, continuity flag upgrade via bible terms, zero-key Arabic
e2e with Arabic pack), descriptor regression, harvested fixtures validated.

**Next**: M5: media providers (mock image/video), prompt adapters, cache, chaos, budget guard,
recovery strategies, scene scheduler, keyframe + generate nodes.

---

## M4c: Creative/production quality fixes (2026-11-04)

**Built**: shot lints (one primary action; negative-prompt contradictions) wired into the storyboard and
prompt `convert` → repair; `insight` field and rubric weight; `single_shot_plausible` judge flag
with a −1.5 penalty; deterministic Gulf dialect fallback; prompts v2; `perfume_ar` re-harvested.

**Real re-runs (balanced, auto)**, M4b → M4c:

| Run | Winner before | Winner now | Multi-action / contradictions / repairs |
|---|---|---|---|
| AR perfume | النصب (scale-trick monument) | قبل لا توصل (the room senses him before he arrives) | 1 / 0 / 0 → 0 / 0 / 0 |
| EN coffee | The Lattice Hour (drink montage) | The Last Sip of the Evening | 0 / 0 / 0 → 0 / 0 / 0 |
| AR delivery | كشتة ما نسيت شي | المعدة تهز البيت (hunger as an earthquake) | 1 / 1 / 0 → 0 / 0 / 0 |

Time 74–96 s, $0.15–0.16 per run. Dialect for "أبي إعلان…" is now `saudi_najdi`.

**Tests**: 307 passed, 2 skipped. New: lint cases from verbatim real-run text (busy and fine),
contradiction and legitimate-negative cases, dialect, judge insight/plausibility, fixture lints.

**Next**: M5.

---

## Pre-M5 fix: product presence (2026-11-04)
Product must be shown or teased in ≥2 scenes with the hero last: deterministic lint → repair, plus
judge flag `product_only_at_end` (−1.0); prompts v3. Real perfume re-run: the bottle is teased in
S2/S3 before the S4 hero (ADR-0014 addendum). `perfume_ar` re-harvested. 311 tests passed.

---

## M5: Media, recovery, chaos, budget, scene scheduler (2026-11-04)

**Built**: `providers/media/{base,mock,chaos}.py`, `providers/adapters/base.py` (kling / seedance /
veo / mock), `agent/media.py` (MediaGateway), `agent/scenes.py` (SceneStageRunner + strategies),
`agent/qa.py` (MockCritic), `agent/nodes/{rewrite,assemble}.py`, `prompts/prompt_rewrite/v1.md`,
budget reserve/settle/release, `approve_storyboard`, CLI `--chaos` / `approve` / `history` /
`resume --from-version`. Decisions are in ADR-0015.

**Evidence (zero-key CLI runs)**
- Chaos (seed 66): S4 transient → backoff retry; S3 policy → LLM rewrite (keyframe kept) → attempt
  2 ok; S2 quality → QA rejected 0.45 → revision with the critic's fix → regenerated → approved; S1
  untouched. Result: `done`, 4/4 approved, 15 s.
- Isolation (`2:clip:permanent`): S2 failed with a plain reason; S1/S3/S4 approved; `done_partial`;
  11.0 s ad (failed scene skipped).
- Cache: forking the same storyboard (from the pre-keyframe snapshot) → **0 provider calls, 8 cache
  hits, $0 media** (original: 8 calls, $1.892).

**Bugs/UX found while demoing (fixed)**: scene-stage `stage_completed` showed $0 (it ignored
media spend); "attempt 2" in clip events counted the keyframe attempt; the mock badge overlapped the
heading; the CLI pointed to the CAS path instead of `runs/<id>/final.mp4`.

**Tests**: 342 passed, 2 skipped. 6 s total, with real mock media at reduced size. New:
`tests/integration/test_scene_pipeline.py` (12 end-to-end recovery/isolation/budget/cache/gate/
resume tests) and `tests/unit/test_media_units.py` (chaos determinism, fail plan, duration
snapping, adapters, budget ledger).

**Next**: M6: LLM vision QA critic (frame sampling).

---

## M6: Vision QA critic (2026-11-04)

**Built**
- `media/frames.py`: frame sampling at 15/50/85% of the *played* clip (scene duration, not the
  snapped render length), JPEG ≤768 px; keyframe re-encode.
- `agent/qa.py`: `LLMCritic` (keyframe + 3 labelled frames + shot spec + scene bible sections +
  prompts → `QAVerdict`), `check_verdict` (failed check ⇒ matching issue; `frame_index` in range →
  repair), `frames_inspected` set by code. Wired via `qa_mode` (`auto` → LLM with real media).
- `prompts/qa_critic/v1.md` (severity and score anchors, fixes phrased as prompt edits, category
  decides keyframe reuse). Packaged `qa_critic` fixtures for both scripted scenarios.
- `StructuredCaller` accepts interleaved text labels with images; `ScriptedLLMProvider.override`.
- `CacheStore.evict`; QA rejection evicts the rejected clip (and keyframe for look issues);
  keyframe retries get `seed = attempt_no - 1`.

**Key decisions** (ADR-0016): judge only the played part; LLM fills fields, code decides; consistency
rules go through repair; known-bad outputs never served from cache; cross-scene reference image
deferred until real output shows whether it's needed.

**Bug found by the new tests**: a product/look rejection whose rewrite kept `keyframe_prompt`
unchanged re-served the **rejected keyframe from cache** at $0 (same request hash), so the
regeneration was a no-op and would be rejected again. Fixed by eviction + per-attempt seed; the
test asserts the identical-prompt retry is a real generation and the bad entries are gone.

**Not done on purpose**: no live critic run. Mock frames would always fail, so calibration is M11 step 2
on real LTX output.

**Tests**: `make check` green, **358 passed, 2 skipped** (live). New
`tests/integration/test_qa_critic.py` (12): sample times / played duration, bounded JPEG decode
from a real mock clip, verdict rules, what the model receives (4 JPEGs, labels, prompts), motion
rejection keeps keyframe, product rejection regenerates it (new seed, cache evicted), repeated
rejection fails only that scene, inconsistent verdict → repair, critic error isolated, packaged
scenario end to end with `qa_mode=llm`.

**Next (reordered by the user)**: M11 fal provider (LTX-2.3 Fast dev model + Kling v3 Pro + Flux,
endpoints verified from fal docs), then M7.

---

## M11 (in progress): fal provider, real S4 calibration (2026-11-04)

**Built so far**
- `providers/media/fal.py`: direct queue REST client (no `fal-client`: it retries internally,
  including submits). One request per step, errors classified. A transient error after submit keeps
  the handle, so a retry *resumes* that request and never resubmits it (no double spend).
- `storage/account.py`: cross-run wallet ledger (reserve → settle/release in `BEGIN IMMEDIATE`). A
  submit is refused if it would exceed the prepaid balance. An ambiguous submit timeout keeps the
  reservation open. Fal runs get budget = min(run cap $5, wallet remaining).
- Endpoints and prices verified from fal's OpenAPI schemas and pricing API (models.toml). Prices
  differ from the model pages (LTX $0.06/s vs $0.04/s on the page; Kling $0.14/s vs $0.112/s); we
  budget with the higher figure. LTX-2.3 Fast renders 6–20 s only, and neither LTX nor FLUX dev has a
  negative-prompt field. Adapters `kling`, `ltx`.
- Prompts plan motion for the visible window (render 6 s, show 3.5 s); literal-noun rule for keyframe
  and motion prompts (`prompt_writer` v4, `prompt_rewrite` v3); code decides keyframe reuse and
  enforces it on the rewrite.
- QA: stricter code pass rule; rule applied at decision time (an older approved snapshot became
  unloadable when the rule tightened; fixed and tested); `qa_critic` v2 (keyframe checklist,
  `product_visible` = readable in every shown frame) and v3 (bible-derived atomic product checklist).
- `commands.edit_scene_spec`: minimal scene edit (bump version → stale from prompt → re-prompt only
  that scene). First real use: the S4 simplification. The full NL edit flow is M7.
- Scripts: `fal_scene.py` (one real scene through the pipeline; `--loop`, `--reprompt`,
  `--edit-json`, `--stop-on-reject`) and `qa_calibrate.py` (critic versions on identical stored inputs).

**Real S4 (hero bottle) results**

| Try | Video | What happened | Critic | fal |
|---|---|---|---|---|
| 1 | LTX | curtain rendered as a veil over the bottle | v1 reject 0.50 | $0.385 |
| 2 | LTX | clean bottle, shadow action missing | v1 pass 0.72 (fails new rule) | $0.36 |
| 3 | LTX | new keyframe (light side fixed; round cap + collar lettering); veil again | v2 reject 0.45 | $0.385 |
| 4 | Kling | no veil, crisp product, shadow ignored, smoke wisp; keyframe light/plant wrong | v3 reject 0.45 | $0.585 |
| 5 | Kling, simplified spec | best so far: squared cap, clean push-in; prop with garbled letters, lower fifth busy, smoke | v3 reject 0.50 | $0.585 |

Wallet (ledger estimate): $10.00 − $2.30 = **$7.70** remaining. LLM (Anthropic) for critic and
rewrites in these runs: about $0.39.

**Known limitations recorded**: critic non-determinism (0.72 vs 0.62 on the same clip; mitigation:
second opinion near the threshold); fine product identity (round cap missed 2/2). Both in the
ADR-0016 addendum. "More time" list started in `docs/MORE_TIME.md` (escalation ladder,
product reference image, keyframe-model adherence, "dust motes" → smoke).

**Next**: step 4 (full 4-scene ad) after review: model choice, the keyframe-model problem, and the
budget plan.

### M11 update: keyframe gate, keyframe model A/B, first full real run (2026-11-04)
- **Keyframe gate built** (ADR-0003 addendum): `qa_keyframe` v1 reviews the still alone; reject →
  revise keyframe prompt, new seed, evict, max 3 tries, then fail without video spend; only a
  gate-approved keyframe (sha256) is animated. Post-clip `qa_critic` v4 judges the clip against
  the approved keyframe. `prompt_writer` v5 / `prompt_rewrite` v4: no atmospheric particles in
  product shots unless the spec asks. Per-endpoint image params. 410 tests.
- **Keyframe A/B (simplified S4, ≤2 tries each, same prompt)**: FLUX dev FAIL 0.50 → PASS 0.80
  ($0.05); flux-2-flex FAIL 0.50 → FAIL 0.62 ($0.10). FLUX dev stays the default (passed, half
  price). flux-2-flex got the squared cap right 2/2 vs 1/2 for FLUX dev, but its tabletop reflections filled
  the tagline area. n=2: weak evidence.
- **Full 4-scene run on Kling with the gate** (`run_01M6JDRTH4FR6AA2D31QM5RZ8D`): **0 of 10 reviewed
  keyframes approved, no video generated, no final video**; `done_partial` with 4 failed scenes.
  Manual check agrees with the rejections (S1 doorway on the wrong side, S2/S3 bottle large in
  the foreground, smoke from the bottle, a label-like mark, S4 light side / busy lower fifth). Spend:
  $0.25 fal (keyframes) + $0.32 Anthropic. Without the gate, each rejected still would have been
  animated first: ~10 × $0.56 = $5.60 of Kling clips (the $4.85 run budget would have stopped it
  first).
- **fal account refused one call mid-run** with HTTP 403 "User is locked. Reason: Exhausted
  balance". Later calls in the same run succeeded. Our ledger says $7.30 remains, so the real
  balance and our estimate disagree; the dashboard is the source of truth. The scene hit by the 403
  failed as `budget_exceeded`; the others continued (isolation held).
- **Finding**: the bottleneck is now the keyframe model's adherence (layout, light side, prop
  placement), not the video model or the critic. "Dust motes → smoke" could not be tested (no
  clips were produced).

### M11 final: severity calibration + final real ad (2026-11-04). Real-media work FROZEN after this.
**Built**: account-level provider refusal pauses the whole run (`AccountLockedError` → fal circuit
breaker + scene back to pending + run `errored`, resumable); hard/soft QA severity split
(`qa_keyframe` v2, `qa_critic` v5, `text_free` check; soft categories capped at minor by code;
pass = no hard failure, no blocker, score ≥ threshold); `scripts/gate_recalibrate.py`. 414 tests.

**Calibration story** (ADR-0016 addenda 1–3): too lenient (a clip with 2 majors passed at 0.72) →
stricter rule + critic v2/v3 + keyframe gate → too strict (10/10 keyframes rejected in the first
full run) → hard/soft split. Re-judging the 14 stored keyframes: 1/14 → 11/14 passing; my eyeball
check says 12/14 of the new verdicts are right (the 2 misses are false passes on a round cap).

**Final real ad** (`run_01M6JF4T2D78QNQJ7ZSE8GA8WR`, Kling v3 Pro + FLUX dev + keyframe gate,
fal hard cap $3.00 via the wallet ceiling): `done_partial`, **3/4 scenes**, 11.0 s, 720×1280:
`data/runs/run_01M6JF4T2D78QNQJ7ZSE8GA8WR/final.mp4`.

| Scene | Keyframe gate | Clip QA | fal |
|---|---|---|---|
| S1 man leaves the majlis | PASS 0.78 (try 1) | PASS 0.72 | $0.585 |
| S2 ghutra, dallah, bottle tease | FAIL 0.45 (round decanter: hard identity) → PASS 0.72 | PASS 0.72 | $0.61 |
| S3 curtain leans to the door | PASS 0.74 | FAIL 0.62 → FAIL 0.50 (smoke rising out of the bottle cap) → regeneration refused by budget | $1.145 |
| S4 hero push-in (simplified spec) | PASS 0.80 | PASS 0.80 | $0.585 |

fal this round: $2.925 (cap $3.00); Anthropic $0.28. Real recoveries: S2 keyframe gate reject →
prompt revised from the critic's fixes → new keyframe passed (no video spent on the bad one,
~$0.56 saved); S3 two QA regenerations with motion-only rewrites (keyframe kept); then the budget
guard refused the third clip and the scene failed in isolation; assembly skipped it.

**Honest critique of the final video.** The look is consistent (warm golden-hour majlis, amber/brown
grade) and the story still reads (he leaves → his things linger → the bottle), but the S3 "lingering
presence" beat is missing. Same bottle? *Same family, not identical*: S2 and S4 both show a
rectangular amber bottle with a squared gold cap, but S2's is more faceted and decanter-like and
S4's has a taller ribbed collar. Same man? Only S1 shows him (thobe, ghutra, agal match the bible;
he half-turns and his beard shows, where the bible says his face is never clearly shown). S1's
doorway shifts between keyframe and clip. No end card or music yet (M8).

**LTX-2.3 Fast vs Kling v3 Pro** (same S4): LTX is cheap and fast (~35 s, $0.36 for a 6 s minimum
render) but rendered a passing shadow as a veil over the product 2/3 times and its push-in was barely
visible. Kling is slower (4–8 min, $0.56 for 4 s), keeps the product crisp and stable, has real
parallax, but ignores light-only actions and adds smoke when particles or smoke are mentioned near the
product (S3 again; S4 with no particle words had none: weak support for the "dust motes → smoke"
hypothesis).

**Keyframe gate savings**: across M11 the gate stopped 21 keyframes before animation (~$0.56 each
on Kling). The 10 in the first full run were mostly false rejects of the too-strict gate; with the
calibrated gate, 1 of 5 was rejected, correctly.

**Main known limitation**: keyframe-model layout adherence (FLUX dev ignores light side, prop and
doorway placement, and sometimes product shape), plus fine product-identity perception in the critic.
Fix: product reference image (MORE_TIME.md #3, #7).

**Wallet (ledger estimate)**: $10.00 − $5.625 = **$4.375**. The dashboard is authoritative; one
"exhausted balance" 403 was seen earlier while the ledger showed $7.30.

**Next**: resume the plan at M7 (edits), M8 (assembly + Arabic end card), M9, M10, M12, M13, with
mock media for the demo flow plus the real clips above.

### M11 addendum: capability profiles + single-take hero format (2026-11-04)
Built per ADR-0017 (`ideate` v4, `select` v4, `storyboard` v4, 1-scene validators). 419 tests.
**Real test** (`run_01M6JH38ZT2WRDPSQ2GM833F7T`, request "إعلان 10 ثواني … لقطة وحدة متواصلة …
والمنتج يطير نحو الكاميرا", Kling v3 Pro, gate on): three single-take concepts (silk-curtain
corridor 7.45; "the friend who borrows your perfume" toss 6.65, producibility 4 for face and hands;
domino glass slabs 6.90). Winner: silk corridor. Keyframe gate PASS 0.78 (1st try), clip QA PASS 0.72
(1st try), no recoveries. **fal $1.425** (cap $2.50), Anthropic $0.22, ~11 min.
Final: `data/runs/run_01M6JH38ZT2WRDPSQ2GM833F7T/final.mp4` (10.0 s).
Honest read: perfect product identity within the take (no drift, no text, no smoke), elegant, but
the promised beat did not happen. The bottle never flies or fills the frame; it stands on the floor and
grows modestly as the camera moves through the curtains, and the amber backlight fades by the end.
QA passed it because missing motion is soft under the current calibration. The cap renders as a
matte black cylinder vs the bible's cuboid gunmetal cap (the known cap-shape blind spot).
**Conclusions (logged in ADR-0017 findings):** the single take is cheaper ($1.425 vs $2.925) and
fully consistent (identical product for the whole take vs bottle drift across scenes), but the
judge played it safe (chose producibility 8 / insight 5 over insight 9 / producibility 4), and a
missing key action passed QA as "soft". Follow-ups noted, not built. **Next: resume the plan at M7
(edits).**

---

## Plan update (2026-11-05)
Order to finish: M7 → M8 → M9 → M10 → M12 → M13. **M12.5 (evals) is cut** for time. Mock media
plus scripted/real LLM only; no fal spend unless the user approves it.

## M7: Edits (2026-11-05)
**Built**: `agent/edit/{refs,blast,service}.py`, `prompts/edit_parser/v1`, `conflict_check/v1`,
`prompt_rewrite/v5` (`spec_edit`), CLI `adsmith edit <run> "<text>"` with a blast-radius table
(depth, provider calls, cache hits per scene). Scripted provider: `"when"`/`"when_all"` steps and a
spec-driven `scene_prompt` transform, so zero-key re-prompts behave like a real LLM. ADR-0005 addendum.
**Plan examples, end to end (mock media, majlis scenario):** "خل المشهد الثاني أبطأ" → S2 clip only,
1 video call, keyframe kept, S1/S3/S4 0 calls · "make scene 3 at night" → S3 prompt depth + S4
propagated by the conflict check (S2 is a separate moment), 2+2 calls, S1/S2 0 · "غيّر لبسه لثوب
أسود" → bible v2, S1–S3 (shows_character) regenerate, S4 product insert 0 calls.
**Found while demoing**: a cross-process scripted replay served stale prompts (fixed with `when_all`
markers); matching against the cached CONTEXT made every scene look like scene 1 (fixed: match the
USER section only); the mock $5 budget (priced at Kling rates) ran out after three edits, which is
the guard working (the demo uses `ADSMITH_BUDGET_USD=15` for simulated money).
**Tests**: 433 passed, 2 skipped (11 in `tests/integration/test_edits.py`).

## M8: Assembly + Arabic end card (2026-11-05)
**Built**: `media/endcard.py` (reshaper + bidi per logical line, missing-glyph guard),
`media/ffmpeg.py`, rewritten `assemble` (normalise → skip or slate failed scenes → end card
dissolving in over the blurred last frame → concat → optional user music → ffprobe duration check),
`endcard_copy` node (fictional brand + tagline when the brief has none; real-brand denylist →
repair; PLACEHOLDER in trace + Production Pack; copy kept in run state across edits),
`commands.reassemble` + CLI `adsmith assemble <run> [--partial slate]`. 446 tests.
**Found by looking at the render**: Tajawal (first pick), Cairo and Almarai lack ~56 Arabic
Presentation Forms the reshaper emits → boxes. Switched to Noto Naskh Arabic (OFL, full
coverage) and added a guard + test covering every letter in every joining form. Also fixed: the
dissolve plate showed the accent rule; a dark-only palette gave low-contrast gold text (now cream).
**Real ads re-assembled (ffmpeg + one Haiku call each, $0 fal)**: majlis 3-scene →
`data/runs/run_01M6JF4T2D78QNQJ7ZSE8GA8WR/final.mp4` (13.0 s, placeholder «أثر المجلس — العطر اللي
يبقى بعدك، والناس تسأل عنك»); single take → `data/runs/run_01M6JH38ZT2WRDPSQ2GM833F7T/final.mp4`
(12.0 s, placeholder «نبض الليل — يحسّون بك قبل لا تدخل»).
**End card resized for phones (M8 follow-up)**: brand ~2× (26% of the width, shrunk only to fit),
tagline ~1.6× (8.3%), 8% margins. Both real ads re-assembled and checked by eye.

### Real defects found by eye that the critic PASSED (targets for the "hero ad" pass; not fixed yet)
- **Majlis S1**: a doorway/arch appears in the right wall mid-clip that is not in the keyframe
  (scene geometry not stable); the ghutra drapes unnaturally.
- **Majlis S2 vs S4**: different bottle shapes (cross-scene product identity).
- **Single take**: the bottle (round body, tall black cap) reads like a liquor/whisky bottle. For a
  Saudi perfume ad this is a brand-safety failure and must be a HARD check.
- **Proposed critic additions**: (1) objects appearing/disappearing between the keyframe and the
  clip frames; (2) wardrobe and anatomy plausibility (drape, hands); (3) a HARD check "the product
  must not resemble an alcohol bottle or drink" (keyframe gate and clip QA). See MORE_TIME.md #9–11.

## M9: CLI + one-command demo (2026-11-05)
**Built**: `cli/live.py` (live Rich trace tailed from the event store: stages, scene progress,
retries, rewrites, QA verdicts, cache hits, edits, cost; also `trace --follow`), rewritten
`cli/main.py` (live output for run/resume/choose/approve/edit/retry/assemble; new `retry`, `list`,
`demo`, `serve` hook for M10), `cli/demo.py` + `make demo`: Arabic request `--auto --chaos` →
transient retry, policy rewrite and QA regeneration, all labelled → end card → edit "خل المشهد الثاني
أبطأ" (S2 clip only, others 0 calls) → a child run SIGKILLed mid-generation and resumed (clips done
before the kill are not regenerated; $0 reservations leaked). ~16 s total, zero keys.
`commands.retry_scene` (re-arms a failed scene with a fresh attempt budget, keeping an approved keyframe).
**Bugs found by the demo** (fixed + tests): `resume` released the lease of an interrupted scene but
left its attempt open and its budget reservation leaked; the first fix then closed the attempt
without a `failure_kind` (rejected by the domain). Also: demo recovery counts were double-counted,
and the crash run shared the cache with the first run so nothing was left to kill (now its own data dir).
**Smoke-tested commands**: run, choose, approve, run --auto, retry, edit, assemble [--partial slate],
show, trace, export, list, history, resume. `serve` arrives in M10.
**Known limit (fixed right after, see ADR-0015 addendum)**: with real fal, a job in flight when the process dies is still billed and resume
resubmits it (the in-flight handle lives in memory). Fix: persist the fal request id in the account
ledger at submit time and resume polling it (MORE_TIME #12).

## Post-M9: crash-safe fal jobs (2026-11-05)
The fal request id is persisted on the attempt right after submit (before polling); resume polls
that request and settles the dead process's wallet reservation; it resubmits only if fal reports it
gone or failed. Interrupted attempts no longer shift the keyframe seed. Tests: provider level and end
to end (pipeline cancelled after a clip submit, resumed in a fresh runtime: 0 duplicate submits).
ADR-0015 addendum; MORE_TIME #12 done.

## M10: Web UI (2026-11-05)
**Built**: `api/app.py` (FastAPI: REST actions, SSE trace tailed from the event store, Production
Pack zip, media served from `cas/` and `runs/` only), `api/view.py` (one view model per run),
`api/gallery.py` + `config/real_runs.toml` (majlis ad, single take, curtain before → after), static
single page (`api/static/`: dark cinematic design, serif display + sans body, bundled Noto Naskh for
all Arabic via `unicode-range`, `dir=auto` on user content). Flow: request → live pipeline →
3 concept cards (insight, judge score bar, producibility/originality/cliché flags, judge's pick) →
storyboard with status chips, per-scene recovery chips and hover-to-play clips → edit box with the
blast radius drawn on the cards (depth tag + glow, untouched = $0) → final player + end card +
Production Pack. Side panel: budget meter, live trace with filters (recoveries/QA/cost/edits).
**Changed while reviewing the screenshots**: the judge now scores concepts in interactive mode too
(recommends; the human picks), mock keyframes became text-free abstract cinematic frames (the old
text cards showed Arabic as boxes), blast tags got solid backgrounds, "Apply edit" no longer wraps.
**Security bug found by a test**: `/files/` served anything in the data dir, including the SQLite
DB; now media folders and media types only.
**Screenshots**: `docs/screenshots/{1_concepts,2_storyboard_midrun,3_final_player,
4_edit_blast_radius,4b_edit_done,5_real_runs_gallery}.png` (Playwright + installed Chrome,
`scripts/ui_screenshots.py`). 455 tests.

## M10 review + M12: README (2026-11-05)
**UI review as a first-time evaluator** (Playwright walkthrough at 1600 and 1280 px; screenshots in
`docs/screenshots/review/`, local only): fixed gallery poster frames (the before/after pair showed
identical first frames), a "made with real models" teaser on the landing page, a "simulated" label on
the mock budget, no absolute local paths in the trace, an honest pick hint (user vs agent), compact
scene meta at 1280 px, a favicon, and the mock clip debug badge moved behind `ADSMITH_MOCK_DEBUG_BADGE`. Left:
mock keyframes all read as silhouettes (mock by design). **Process slip:** one commit went in with a
red lint (`;` instead of `&&`); amended locally before anything else.
**README** rewritten for a 3–5 minute skim (pitch + hero screenshot, two quickstarts, the brain with
3 Mermaid diagrams, error matrix, edits table, "what real runs taught me", ADR one-liners,
limitations, repo map). Real clips committed under `docs/media/` (~9 MB) so the README links work.

## Hero round: critic fixes, spray rule, best-of-N, real run (2026-11-05)
**Built:** HARD `alcohol_free`; `key_action_happens` (HARD for single takes, set by code); `geometry` and
`anatomy` issue categories (never soft-capped); `qa_keyframe` v3, `qa_critic` v6. Spray/mist allowed
when it IS the action (`prompt_writer`/`prompt_rewrite` v6 + shot lint: a spray action makes mist
required, so a negative prompt banning it gets repaired; smoke, haze and dust stay bannable). Best-of-N clips
(`clip_candidates`, `--hero` = 2): N parallel jobs from the same gated keyframe, all reserved up front,
code picks the best score with no hard failure, losers kept in the CAS; none passes → scene fails
(ADR-0003 add. 2). Kling profile + `select` v5: people in natural motion are not a producibility
penalty (ADR-0017 add.). Gallery `kind = "hero"`. 468 tests.
**Real run** `run_01M6KJYGMWF7RHQY8B86T5G7JK` (cap $3.00 via wallet ceiling 10.05 = ledger 7.05 + 3.00):
concepts يسبقك 7.95 (picked), حرير الرذاذ 6.35, وش عطرك؟ 6.10. Gate: 0.45 ✗, 0.50 ✗ (wrong bottle),
0.78 ✓. Best-of-2 Kling 8 s: 0.35 ✗ / 0.40 ✗ (identity, invented label text, "no key action") →
scene failed, nothing assembled. **fal $2.315** (stills $0.075 + 2 × $1.12), Anthropic $0.33.
**By eye:** text and cap-colour rejections are correct (the blank "label plate" became invented
lettering in the close-up); "key action missing" is WRONG (a short spray burst is visible at ~2–3 s
in both takes); the gate let the black cap through as a "minor" product issue, which is the real
miss. Energy: a calm walk, not the slow-motion spray moment the request asked for.
**Next (not built):** product checklist mismatch at the gate → hard (no "minor product" issues); no
label plates in product bibles (wordless glass, nothing to fill with text); a slow-motion wording
lever for Kling; store full candidate verdicts in the event (only reasons are stored today).

### Hero-round fix: product mismatch is hard at the gate (2026-11-05, Anthropic only)
`gate_draft()`: any "product" issue at the keyframe gate is major and fails `continuity_ok` (code,
not prompt); `qa_keyframe` v4 keeps size/placement under "composition". `continuity` v2 + node check
`wordless_label_problems()`: a wordless product may not describe a label, plate, plaque or tag
(→ schema repair; negated mentions such as "no label" are fine; stored bibles still load). Re-judged
the stored hero keyframes (`scripts/gate_recalibrate.py`, $0.05): a3 **PASS 0.78 → FAIL 0.45** for
the black cap; a1/a2 still fail. Real-media work frozen for good.

## M13: fresh-clone check + demo script (2026-11-05)
Fresh clone (`env -i`, no `.env`, no `data/`): `make setup` → `make demo` (23 s: chaos recoveries,
end card, edit blast radius, SIGKILL + resume, $0 leaked) → `adsmith serve` (zero-key badge, Arabic
chaos run to `done`, pack 200). **Fixed:** the Real runs tab and landing teaser were empty in a fresh
clone (data/ is gitignored) → the gallery falls back to the committed clips in `docs/media`
(`/docs-media/{name}`, file names only); the teaser shows 3 cards. History: `.env`, `data/`, `*.db`
never committed; the real key values have 0 matches in `git log -p`. `docs/DEMO_SCRIPT.md`: 4:45 script
(Arabic narration, English terms) + likely interview questions. 470 tests. Done.

## Hero v2: locked product reference, real run (2026-11-05)
**Built:** product reference + FLUX Kontext keyframes (ADR-0003 add. 3), Kling v3 Standard in the
catalog, `adsmith budget`, `qa_max_regenerations` (0 = one take), requested shot count enforced,
no effects as product details, salient-vs-fine identity (`product_detail` soft, ADR-0016 add. 5),
adopt a re-gated packshot, user `shows_product` override, final QA rejection keeps its reasons. 483 tests.
**Run** `run_01M6MAK00158YYA7JN2YBD0VSR` (fork of `run_01M6M9AG90D4ZBB7TE9GHTNZ37` @ v69), concept
«يسبقك ريحك», 3 × 3 s, Kling v3 Standard, one take per shot. Reference = stored packshot 2,
re-judged under the salient/fine gate (0.50 → 0.85, $0 fal). Keyframes: S1 Kontext 0.78, S3 Kontext
0.84 (same bottle as the reference), S2 mist FLUX dev 0.85 (third try; FLUX ignored "horizontal
burst", see MORE_TIME 13). Clips: S2 QA 0.82 ✓, S3 0.84 ✓, **S1 0.45 ✗ "the spritz never happens"**,
but a mist burst is visible at ~2 s: 3 sampled frames missed a brief action (critic error).
Final (partial): `data/runs/run_01M6MAK00158YYA7JN2YBD0VSR/final.mp4`, 8 s (S2 + S3 + end card «نفحة المجلس»,
placeholder). fal ledger for the round $1.765 (cap $1.80; ledger prices Kling at the API's $0.14/s,
page says $0.084/s, so the real bill is likely ~$1.26). Contact sheet: `docs/screenshots/hero_v2_contact_sheet.png`.
**Lessons:** the reference works (identical bottle in S1, S3 and the reference); the gate swung
strict again until salient/fine; brief actions need denser frame sampling around the action.
**Follow-up (same day, no fal):** dense frame sampling for key-action checks (8 frames across the
played window for single takes and brief events like a spray). Re-judged S1 with 9 images: 0.45 →
0.50, still failing; the critic now sees the burst but calls it "faint haze behind the shoulder"
(it goes outward, not at the neck) and reads the tilted bottle as "slimmer". Per the user's rule,
S1 was accepted with the new human override (`adsmith accept`, logged as `HUMAN OVERRIDE` with the
failing verdict kept, shown in the UI) and the ad reassembled with ffmpeg only.
**Final:** `data/runs/run_01M6MAK00158YYA7JN2YBD0VSR/final.mp4`, 11 s, three shots + end card. 486 tests.

## Step A: composite mode, bible patch, flag correction (2026-11-05)
**Built:** composite product-reference mode (plain keyframe at scene scale, then Kontext multi swaps
the reference in; ADR-0003 addendum 4); `patch_bible` for a run mid-edit; `correct_scene_flags`
(human correction of false-positive flags without regeneration). **Run:** fork
`run_01M6MCGVC2YFM2RHRCEWJFZZMP`, S1 as a hand/jawline close-up in composite mode, keyframe only
(`docs/screenshots/stepA_s1_composite.png`), $0.17 fal. Parked at the approval gate and superseded
by PLAN_V2: no clip generated for it.

## PLAN_V2 items 1-4 + STOP 1 tooling (2026-11-05)
**Built:**
- Item 1, formats as data (`config/formats.toml`, ADR-0018): montage / single_take / narrative
  grammars; one format per run resolved by code (`resolve_format`); `Brief.format`,
  `Storyboard.format`; montage shots keep 1.0-1.5 s of a ≥ 3 s render; understand v4, ideate v5,
  storyboard v6; `montage_en` scripted scenario; `max_total_attempts` 80.
- Item 2, shot vocabulary (`config/shot_vocabulary.toml`, 16 entries): `Scene.vocab` in the spec;
  flags set from the entry (no text-match upgrade for vocab scenes); person text in a person-free
  entry is repaired; storyboard v7, prompt_writer v7, prompt_rewrite v7.
- Item 3, identity routing (ADR-0019): per-shot none / direct / composite from the vocabulary;
  the locked reference turns on by itself for montage; keyframe_reference v2 (macro re-frame),
  keyframe_composite v2; identity + vocab in the keyframe attempt events.
- Item 4, wardrobe lexicon (`config/culture.toml`): bible lint (→ repair), continuity v4,
  prompt_writer v8, qa_keyframe v6. The Arabic fixture's own bible said "mandarin collar"; fixed.
- STOP 1 tooling: `scripts/contact_sheet.py`, `scripts/decisions_report.py`,
  `run_report.py --decisions`.
- Fix found before spending: the run budget counted Anthropic calls against the fal wallet
  headroom, so a $0.60 fal cap would have refused the stills. `BudgetLedger.media_limit_usd` now
  holds the wallet cap (image + video only); `ADSMITH_BUDGET_USD` stays the total cap.

**Deviations from PLAN_V2 (agreed at review):** one format per run instead of "≥ 2 montage
concepts, the judge picks"; footage = requested length (end card on top); `fit` per endpoint →
`avoid_on`; `prompt_rewrite` does not carry the wardrobe lexicon yet.

**Tests:** 539 passed, 2 skipped (live). `make check` green at every commit.

### STOP 1 proof run (2026-11-05): `run_01M6MGNPSHZ3GV5TTHN983R022`
Fully automatic (`--auto --until generate`, no human fixes), Kling v3 Standard target, fal media
cap $0.60 (wallet ceiling 11.90), keyframe tries 2. A first attempt stopped at `understand` with a
400 "Schema is too complex" ($0 spent); fixed and every changed schema probed before relaunch.
**Decided by the system:** format montage (physical product, no format asked); concept «حدّ
الزجاج» (judge 5.25 vs 4.85 / 3.80); 10 shots of 1.2 s; reference packshot passed first try (0.84);
8 direct + 2 plain keyframes. **Gate:** 9/10 passed on the first try (0.72-0.85); S9 spray_burst
failed twice (vertical smoke column again, FLUX dev ignores "horizontal burst"; no video money).
**Spend:** fal $0.42 (ledger 11.30 → 11.72, nothing left reserved), Anthropic $0.49.
**By eye:** the bottle is the same in every product shot and the look is the references' dark,
rim-lit world. Weak: Kontext keeps the packshot framing, so the "macro" entries (S1, S3) came out as
the full bottle and five shots look alike; only S2 re-framed partly. The water ribbon (S6) is
missing and S7's bubbles rise above the cap; the gate passed both at 0.72-0.80 (composition is
soft). Sheet: `docs/screenshots/stop1_contact_sheet.png`; decisions: `docs/screenshots/stop1_decisions.txt`.

## STOP 1 fixes + Phase 2 (items 5, 6, 8): STOP 2 readiness (2026-11-05)
**Fixes (from the STOP 1 sheet):** macro entries → composite identity (keyframe_composite v3 macro
branch); composition is HARD for key shots and event shots (`composition_hard`: water_ribbon,
bottle_into_water, spray_from_nozzle, plus key shots; qa_keyframe v7, qa_critic v8); spray_burst
replaced by `spray_from_nozzle` (reference-based, cap off via `reference_note`, which the gate is
told is deliberate; keyframe_reference v3). `edit_scene_spec` can switch a shot's entry;
`set_budget` moves the wallet cap to the media cap.
**Re-roll** on `run_01M6MGNPSHZ3GV5TTHN983R022` (`scripts/stop1_reroll.py`, fal cap $0.28, spent
$0.275, ledger 11.72 → 11.995, nothing reserved). Re-judged under the hard rule (LLM only): water
ribbon 0.50, bubbles 0.45, hero 0.62 (lower fifth not clear), all FAIL. Wave 1: **spray from the
nozzle PASS 0.85** (a real horizontal plume, first try). **Macro composite FAILED**: Kontext multi
returned the studio packshot itself (beige background) for cap_macro and glass_edge_macro; the
previous direct-mode stills were lost with the re-roll. Wave 2: water ribbon re-rolled once, 0.50
FAIL; bubbles and hero kept (no room left under the cap). Sheet:
`docs/screenshots/stop1b_contact_sheet.png`; decisions and re-judge log next to it.
**Phase 2 built:** item 5 peak window (`motion_profile` + `peak_window`, `Scene.use_window`, the
critic samples it, assembly seeks to it; prompt_writer v9); item 6 beat cut (`media/beats.py`:
spectral-flux onsets, on-beat minus off-beat comb tempo, beats within 20 ms on a 120 bpm click
track; `snap_cuts` ±0.2 s, hero on a downbeat, end card on a beat); item 8 best-of-2 for vocabulary
key shots when the headroom covers both takes. ADR-0020. numpy added.
**STOP 2 mock run:** montage_en + a 120 bpm click track: hero best-of-2, 9 shots, "Beat cut: 120
bpm … 9 of 9 shot lengths adjusted", 13.96 s rendered vs 14.00 s expected.
**Tests:** 557 passed, 2 skipped.
**Open:** the macro identity problem (Kontext keeps the packshot framing in direct mode and copies
the packshot in multi mode); `assets/music.mp3` not yet supplied (gitignored).

## Macro crop, vocabulary fallback, generated music + montage run 1 (2026-11-05)
**Built (ADR-0021):** identity mode `crop`: `cap_macro` / `glass_edge_macro` are cut from the
approved packshot in code (`media/crop.py`) and Kontext only relights them onto black
(`keyframe_relight` v1); `cap_lift` is a direct product shot with a reference note. **Vocabulary
fallback**: a shot whose keyframe fails the gate on every try is swapped by the scene runner to
its entry's fallback (new version, fresh prompt, `vocab_fallback` decision), once per shot.
**Generated music**: Lyria 2 on fal ($0.10 per 30 s track, verified on page, schema and pricing
API; $0.15 cap), a provider step before the assemble node (cache → budget category `audio` → CAS
→ `RunState.music`); any failure assembles without music. Spray and water event shots are key
shots (best-of-2 when the budget covers it; other key shots count at two takes).
**Montage run 1** (`run_01M6MT0QP80T95Q796V25HE1VD`, `--auto`, Kling v3 Standard, ledger cap
$6.50, keyframe tries 2, QA regenerations 1, no human edits). Format montage (code: physical
product, no format requested); concept «ذهب سايل» (7.10 vs 5.10 / 4.60); 10 shots. Packshot passed
first try (0.80). **Macro crop worked**: cap_macro passed the gate (0.80) and clip QA (0.72). Two
event shots (water_ribbon, bottle_into_water) failed the hard composition gate twice and **fell
back automatically**, both to `bottle_on_rock` (a race). glass_edge_macro (Kontext re-framed the
crop to the whole bottle), droplets_on_glass (the cap turned to crystal) and hero_push_in (its
glossy reflection filled the lower fifth) failed with no fallback left. light_sweep and
rim_light_reveal passed the gate but failed clip QA twice each (Kling ignores light-only events).
Lyria track generated ($0.10); beat cut 130 bpm, 5 of 5 cuts snapped; **done_partial: 5 shots +
Arabic end card, 8.4 s** (`docs/media/montage_v1.mp4`). Ledger: stills $0.705, clips $2.268,
music $0.10 (fal media $3.073), Anthropic $0.89. Sheet and decisions:
`docs/screenshots/montage_run1_contact_sheet.png`, `montage_run1_decisions.txt`.
**Fixed after the run (no spend):** fallbacks decided one at a time under a lock; after the chain,
an unused safe product shot, then a repeat (never a hole); entries another shot failed with are
not offered again; `avoid_on` now enforced (storyboard vocabulary and fallbacks): light_sweep and
rim_light_reveal are avoided on Kling; hero falls back to bottle_on_rock and asks for a matte void
without a reflection. Fixture S7 is now turntable_glint.
**By eye:** the look is right (black, one hard rim light, gold cap, amber liquid) and the bottle
is identical in every shot: identity is solved. But no physical event survived, so it reads as
five still product shots with slow camera moves, two of them near-identical, and it ends on the
cap lift instead of a hero.
**Tests:** 574 passed, 2 skipped.
**Next (one attempt, about $3.4 at ledger prices):** a fresh run with these fixes; the spray
shot passed in STOP 1, so the event shots are where the next run can win.

## Montage v2: events by rule (2026-11-05)
**Built:** the montage grammar asks for at least 3 physical-event shots (`min_event_shots`, the
vocabulary's `physical_event`: spray from the nozzle, droplets, water drop, splash, ribbon) and the
`hero_push_in` last (`last_vocab`); entries that passed on real media are marked PROVEN in the
storyboard prompt (spray 0.85, drop on a leaf, droplets). Light alone is no longer listed as an
event. v1's final kept as `docs/media/montage_v1.mp4`.
**Run** (`run_01M6MVHD6CE5V22VM38GXNRN09`, `--auto`, Kling v3 Standard, ledger cap $3.80 incl.
LLM, keyframe tries 2, QA regenerations 1, no human edits): 8 shots: spray_from_nozzle,
water_drop_leaf, droplets_on_glass, cap_macro, bottle_into_water, water_ribbon, turntable_glint,
hero_push_in. Packshot passed first try (0.82). Spray: the gate rejected try 1 (cap still on), try
2 passed 0.80; **best-of-2 clips, 0.78 / 0.82**: a real horizontal plume. Drop on a leaf 0.72,
droplets 0.80, cap macro (crop) 0.88, turntable 0.80. The splash and the ribbon failed the hard
composition gate twice and **fell back automatically** (bottle_on_rock 0.82; cap_lift, whose clip
failed QA 0.45 and whose retake was refused by the budget). Hero: try 1 rejected (reflection), try
2 on a matte void passed; **best-of-2, 0.82 / 0.82**. **done_partial: 7 shots + Arabic end card,
12.5 s** (`docs/media/montage_v2.mp4`). **No music:** the track was reserved at assembly and the
cap was gone (the hero's second take was decided while the cap-lift clip was in flight, so it
was not counted). Ledger fal $3.090, Anthropic $0.772. Sheet and decisions:
`docs/screenshots/montage_v2_contact_sheet.png`, `montage_v2_decisions.txt`.
**Fixed after the run (no spend):** the track is made when the generate stage starts (it depends
only on the brief and format), so retakes can no longer take its budget; test added.
**By eye, v1 vs v2:** v2 follows the references (events first, hero last, full 12.5 s) and opens
on the strongest shot of the project, the nozzle spray. v1 has perfect identity and music but
plays as 5 near-still product shots in 8.4 s. v2's flaws: silent; the droplets shot has a clear
crystal cap (a gate miss on a small part); the cap's shade shifts between gunmetal and champagne.
**Tests:** 578 passed, 2 skipped.
