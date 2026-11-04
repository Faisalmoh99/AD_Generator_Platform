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
