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
   `identity = "photo"`, 3–4 shots of 1.5–6 s (was 3–6; cut to 4 for the 8 s demo, 2026-11-06), hero last. `resolve_format` picks it by code when
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

## Addendum (2026-11-06): human creative direction at the spend gate

`adsmith approve <run> --prompt-file <path>` replaces the model-written prompt with a human one,
sent verbatim (`AdGeneration.human_prompt`; the model's `AdPrompt` is kept for the record). Code
checks it first (numbered shots 1..n, contiguous timecodes from 0 to the ad's length, within the
endpoint's prompt limit) and logs a decision event "HUMAN creative direction: prompt replaced"
with a per-section diff (header / Look / Shot N / Audio) and both texts. The run goes back to
ESTIMATE: the new prompt is priced again and waits at the gate. The command then approves only
if the new reservation is not above the one the human already saw; otherwise it stops at the
gate. `--no-approve` re-prices and stops. Reservation, cache key (the prompt is part of it),
critic (its frame windows follow the human shots) and assembly are otherwise unchanged.
`--endcard-tagline` sets the brief's tagline, so the end card shows it (Arabic is shaped by
`arabic_reshaper` + `python-bidi`).

## Addendum (2026-11-06): before try 2

- **Observed charge.** Try 1 was billed $2.06 (console $5.00 → $2.94) against a computed $2.419.
  `adsmith settle-actual <run> --usd X` books the console amount on the wallet ledger (by the
  request id), the run budget and the attempt, with a decision event. The token-formula margin is
  now 0: the computed price is an upper bound (ADR-0022 addendum).
- **Export size.** A multishot final keeps the generation's own size (720x1280 at 720p); the
  project long side (960) applies only when the artifact has no recorded size.
- **Prop references.** `--ref-image` (repeatable, order kept) adds prop images: `image_urls` =
  [product photo, prop 1, ...], so prompts can say Image 1 / Image 2. They are part of the cache
  key. `qa_ad_critic` v2 receives them after the product photo, labelled as props: a prop is
  never compared with the product; a prop that looks like the product (or the product shown in
  the prop's place) is a major "product" issue.

## Addendum (2026-11-07): human retime edit

`adsmith retime <run> --window A-B --trim C-D --factor F --method mci|blend --output NAME`
records a `RetimeEdit` on the generation (frame-aligned; code checks the slowed window adds
exactly the frames the trim removes, the trim follows the window, no overlaps) and logs
"human edit: retime". The run re-assembles: the video is retimed by segment (ffmpeg
`minterpolate` mci or blend on the window), the audio stream is copied untouched, the film keeps
its length and every timestamp after the trim; the output goes to NAME next to `final.mp4`
(earlier finals are kept). A new generation drops the edits. No generation, no spend.
