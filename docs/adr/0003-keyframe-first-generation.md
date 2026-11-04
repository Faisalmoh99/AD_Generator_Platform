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
