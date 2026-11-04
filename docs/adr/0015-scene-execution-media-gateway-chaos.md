# ADR-0015: Scene execution: media gateway, recovery strategies, chaos, budget reservations

- Status: Accepted (2026-11-04)

## Context
Keyframe, clip and QA work is per scene, slow, paid and failure-prone. Requirements: one scene's
failure never blocks the others; every failure kind gets its own bounded strategy; unchanged
work is never paid for twice; spend never exceeds the run budget even with concurrent scenes; the
demo can show recovery live; and all of it must be reproducible in tests.

## Decision
1. **SceneStageRunner** (`agent/scenes.py`) runs each scene as an independent asyncio task
   (bounded by `scene_concurrency`). Every state change (start + budget reservation, success,
   failure, rewrite, QA verdict) is **its own atomic commit** with its events, and checks the scene
   version it was computed for. An edit in between discards the result (the media stays cached).
   A worker that crashes on a bug marks only its own scene failed.
2. **MediaGateway** (`agent/media.py`) executes one generation: **cache → chaos → provider → CAS →
   cache**. A cache hit costs $0 and cannot be faulted. Failures and quality-faulted outputs are never
   cached. Prompt adapters map the provider-agnostic `ScenePrompt` onto each model (duration snapping,
   negative-prompt folding, length limits).
3. **Recovery strategies** map one-to-one onto the taxonomy (ADR-0004):
   transient/429 → full-jitter backoff *inside* the attempt; content policy → shared LLM
   `prompt_rewrite` (≤2, then the scene fails); QA rejection → the same rewrite step fed the
   critic's concrete fixes (≤2; motion-only fixes keep the keyframe byte-identical so it's
   reused); permanent/exhausted → the scene fails with a user-language reason; budget → refused
   before the call.
4. **Budget reservations**: estimate → `reserve` (inside the atomic "start" commit; reserved
   money counts against the limit, so concurrent scenes can't jointly overshoot) → `settle(actual)`
   or `release`. Mock media is priced at the configured fal rates and obeys their duration rules,
   so the guard behaves exactly as it will with real money.
5. **Chaos Mode** is deterministic: a fault is a pure function of (seed, scene order, version,
   phase, attempt, transport try). That makes it independent of task scheduling and reproducible,
   and every injected fault is labelled in the trace. `first_attempt_only` guarantees recovery a fair
   chance. Seed 66 is chosen so a 4-scene demo shows one transient, one policy and one quality
   fault. A separate deterministic **fail plan** (`ADSMITH_MOCK_FAIL_PLAN="2:clip:permanent"`)
   reproduces any specific path.
6. **Gate**: interactive runs stop after keyframes (`awaiting_storyboard_approval`); video spend
   only starts after `approve`.
7. **Assemble**: approved clips are trimmed to scene durations, normalised and concatenated. Failed
   scenes are skipped, so the run ends `done_partial` with a watchable ad.

## Alternatives considered
- One big transaction per stage: simpler, but loses per-scene durability and live progress.
- Retrying policy rejections verbatim, or regenerating from scratch on QA failure: wastes money and
  usually fails the same way.
- A random (unseeded) chaos monkey: demos and tests would not be reproducible.

## Consequences
- + Proven by integration tests with real mock media: transient retry, exhausted transient,
  policy rewrite (bounded), QA regeneration (bounded), permanent failure → `done_partial`,
  budget refusal before calling, cache → 0 provider calls, crash → resume.
- − Each scene commit re-serialises the whole run (fine at our scale, ADR-0011).
- − The mock critic only detects injected faults. The LLM vision critic is M6.
