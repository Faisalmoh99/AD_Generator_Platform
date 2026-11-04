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
