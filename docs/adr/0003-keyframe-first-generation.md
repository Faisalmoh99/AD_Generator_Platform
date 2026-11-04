# ADR-0003: Keyframe-first generation

- Status: Accepted (2026-11-04)

## Context
Text-to-video per scene gives inconsistent characters and products across scenes and is the most
expensive step (about $0.11/s on Kling v3 Pro, so ~$1.70 per 15 s ad before retries). Users also
want to review the look before paying for video.

## Decision
1. Generate one still **keyframe per scene** (text-to-image, ~$0.03) from `keyframe_prompt`
   (composition and look, plus the continuity bible).
2. Gate: **storyboard approval** with keyframes (interactive mode) before any video spend.
3. **Image-to-video** per scene from the approved keyframe, with a separate, short `motion_prompt`
   (camera move and subject action only). The image already carries the look.
4. Invalidation is depth-aware (ADR-0005): motion-only edits (camera move, speed, duration) keep the
   keyframe and redo only the clip.

## Alternatives considered
- Text-to-video directly: cheaper pipeline, worse consistency, no cheap review step.
- One long multi-shot generation (e.g. Kling multi-prompt): fewer calls, but one failure invalidates
  the whole ad, which goes against the per-scene isolation requirement.
- Reference-image conditioning with a product photo: better product fidelity. This goes on the
  "more time" list because it needs user-supplied assets.

## Consequences
- + Cheap visual review; much better cross-scene consistency; cheaper edits.
- + The keyframe is a natural cache boundary.
- − Two provider calls per scene and two failure surfaces (both covered by the same recovery
  layer).
- − Motion is limited by what the I2V model can do from one frame.

## Addendum (M11): keyframe QA gate
**Context.** On real fal output most QA rejections were caused by the *keyframe* (key light on the
wrong side, unrequested props, a filled tagline area, product details wrong). They were only found
after paying for a clip ($0.36 on LTX, $0.56 on Kling), which defeats the point of keyframe-first.

**Decision.** Right after a keyframe is generated, the critic reviews the keyframe ALONE
(`qa_keyframe` template: key-light side, required negative space, unrequested objects, stray text,
framing, and the bible-derived product checklist), with the same code pass rule as clips. A reject
discards the keyframe, evicts it from the cache, revises `keyframe_prompt` from the critic's fixes
(`prompt_rewrite`, reason `keyframe_feedback`) and regenerates it with a new seed, up to
`keyframe_max_tries` (3). After that the scene fails *without any video spend*, and other scenes
carry on. Only a keyframe whose sha256 the gate approved (`Scene.keyframe_approved_sha`) is
animated. The approval survives motion-only clip rejections, so the same still is never judged twice.
The post-clip critic (`qa_critic` v4) then judges motion, product readability in the played frames,
continuity with the approved keyframe, and additions. A clip problem regenerates the keyframe only
if the critic locates it in the keyframe itself.

**Consequences.** + A bad still costs one critic call (~$0.01–0.02) instead of a clip. + Fewer
post-clip rejections, which are more often truly about motion. − One extra LLM call per keyframe
attempt. − The gate inherits the critic's limits (non-determinism, fine product detail; ADR-0016).

## Addendum 2 (hero round): best-of-N clips from one approved keyframe

**Context.** Video models are not deterministic: from the same approved keyframe and prompt, one
take can nail the action and the next can miss it. For the hero ad the costliest failure is a clip
that passes but is the weaker take, or one that fails when a sibling take would have passed.

**Decision.** `clip_candidates` (config, default 1; `--hero` sets 2). After the keyframe passes the
gate, the scene runner submits N clip jobs from the *same* keyframe and prompt in parallel, each a
distinct paid job (`variant` in the fal idempotency key; `candidate` in the request params, so the
cache key differs). All N are **reserved up front** in one write (run budget + wallet), so a hero
scene never starts a take it cannot finish paying for. The critic scores every candidate; the
winner is the highest score among candidates **with no hard failure** (code decides, as always).
Losers stay in the CAS and are recorded as attempts with their verdicts; the trace event carries
`candidates` (score, pass, reasons, clip) and `winner`. If no candidate passes, the scene fails as
usual; there is no extra rescue round (the spend was already 2×).

**Alternatives.** Sequential retry on rejection (cheaper on average, but it only triggers on a
*fail*, never picks the better of two passes, and doubles wall time on Kling's 4–8 min); N = 3
(diminishing returns for +50% cost); asking the LLM to pick (no: code picks from typed verdicts).

**Consequences.** 2× video cost for hero scenes, same wall time. Normal runs are unchanged.

## Addendum 3 (hero v2): locked product reference + reference-conditioned keyframes

**Context.** Every real failure so far was product identity, not motion: text-to-image keyframes
re-imagined the bottle each time (cap colour and shape, glass form), and a blank "label plate"
gave the video model a surface to invent lettering on.

**Decision.** With `product_reference` on (`--product-ref`), the scene runner first makes ONE
packshot of the bible's product (`prompts/product_reference`, default text-to-image model) and
passes it through a strict gate (`qa_product_ref`; any product mismatch or any text/label is hard,
by code via `gate_draft`). Up to `keyframe_max_tries` tries, each retry adding the critic's
`revised_prompt_hint`; rejected stills stay in the CAS. The approved still is stored as
`RunState.product_reference` (tied to the bible version: a product edit makes it stale) and shown in
the UI. Every keyframe of a scene that shows the product is then generated by a
reference-conditioned image model from that still (`prompts/keyframe_reference` wrapper: "keep
this product exactly; new scene: ..."). Its sha256 is the request's `input_image_sha256`, so a new
reference is a new cache key. Scenes without the product keep plain text-to-image. If no packshot
passes, product keyframes fall back to text-to-image (logged as an error event); the run continues.

**Model.** FLUX.1 Kontext [pro] (`fal-ai/flux-pro/kontext`), verified on the model page and
`/api` schema on 2026-11-05: `prompt` + `image_url` + `aspect_ratio` enum, `seed`,
`guidance_scale` 3.5, `safety_tolerance`; no negative prompt or `image_size`; **$0.04 per image**
(page and pricing API agree). Chosen over FLUX.2 [dev] edit ($0.012 per processed MP, up to 4
references), which is cheaper but built for multi-image composition; Kontext is built for keeping one
object's identity while the scene changes, which is exactly this job.

**Brain unchanged.** No new stage or transition: the reference is a step inside the existing
keyframe stage, a new `ImageProvider` (the fal adapter sends `image_url` when the endpoint has an
`image_field`), and an optional `reference` provider on the `MediaGateway`.

**Consequences.** +1 packshot (+ gate call) per run and $0.04 instead of ~$0.025 per product
keyframe; the product can no longer drift between shots at the keyframe level. Off by default so
normal runs and tests are unchanged.
