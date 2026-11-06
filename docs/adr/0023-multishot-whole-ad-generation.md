# ADR-0023: The multishot format: one generation renders the whole ad, behind an estimate gate

- Status: Accepted (2026-11-06)

## Context
Our ads stitched 8–10 separately generated clips (keyframe → gate → clip → QA per shot). Each
shot was a new roll, so identity drifted, physical events failed often, and there was no sound
until music was added at assembly. Higgsfield-quality ads come from ONE Seedance
reference-to-video generation: several numbered shots with cuts and native audio, made from a
real product photo. The user decided the new unit is "one full ad from one generation" (two if
longer than 15 s), that the existing 8-clip path keeps working, and that for this budget (≈ $5)
nothing paid is regenerated automatically.

## Decision
1. **A format, not a fork.** `multishot` joins `config/formats.toml` with `generation = "whole"`,
   `identity = "photo"`, 3–6 shots of 1.5–6 s, hero last. `resolve_format` picks it by code when
   the run has a product photo (`--product-photo`), or when the operator forces it (`--format`).
   The storyboard's scenes stay the numbered shots, so every storyboard invariant and lint holds.
2. **A run-level generation, not a scene.** `RunState.generation: AdGeneration` (prompt, model,
   duration, resolution, aspect, audio, estimate, approval, attempts, video, verdict, version,
   history) with its own transition methods. Scenes get no prompts, keyframes or clips.
3. **Stages.** `PROMPT → ESTIMATE → AWAITING_GENERATION_APPROVAL → GENERATE → QA → ASSEMBLE`
   for a whole-ad run; the scene path is unchanged (`PROMPT → KEYFRAME …`). The branch is a pure
   guard on `state.generation`. `ESTIMATE`, `GENERATE` and `QA` are run by
   `agent/generation_runner.py`, which commits its own atomic updates like the scene runner.
4. **One prompt, checked by code.** `ad_prompt_writer` v1 writes look + numbered shots (start/end,
   action, camera, lighting, sound) + audio; code checks shot count, contiguous timecodes that
   follow the storyboard durations, the ad's length and the prompt limit (→ schema repair), then
   renders the text with fixed rules (identical product, no text, no voice).
5. **Estimate gate.** The provider prices exactly the request; a run's budget or media cap that
   cannot cover it fails the generation before any spend. **Real money always waits for a human**
   (`adsmith approve` / `decline`), even with `--auto`. Code approves only a $0 cache hit or mock
   media in auto mode, and says so in the trace.
6. **Warn only.** The critic reviews the video once: pass → approved, hard fail → flagged. A
   flagged ad is still assembled (it was paid for) and the run ends partial; the human decides
   with `adsmith accept-ad` (logged override) or `adsmith regenerate-ad` (new version, back to the
   estimate gate). A failed generation (not charged at Higgsfield) is recorded, never retried.
7. **Crash safety.** The request id is persisted at submit; `resume` releases an interrupted
   generation (approval kept) and the next `GENERATE` polls the same job.
8. **Assembly keeps the model's sound**: the whole video (re-encoded with its audio), the end card
   with a silent track, joined by stream copy. No music generation and no beat cut.
9. **Edits** on a whole-ad run are rejected with a clear message pointing to `regenerate-ad`: one
   generation renders every shot, so a scene cannot change on its own.
10. **Continuity** v5 receives the real photo as an image, so the bible's product checklist (used
   by the critic) describes the real product.

## Alternatives considered
- Keep scenes and make each one a Seedance call: pays N times and loses the one-generation
  coherence that is the reason for the change.
- Auto-regenerate on a hard fail (as the scene path does): a full ad per retry against a ≈ $5
  balance; the user chose warn only.
- Re-cut the generated video to the planned timecodes: the model decides its own cuts; cutting
  at our marks would chop shots.

## Consequences
+ The 8-clip montage and narrative paths are untouched (no photo → today's formats).
+ Zero-key end to end (mock reference-to-video renders the photo with a tone track).
− Blast-radius edits do not apply to multishot runs yet.
− The planned timecodes are approximate in the real output; the critic samples by time.
− Ads longer than one generation (> 15 s on 2.0) are clamped to the model's maximum for now.
