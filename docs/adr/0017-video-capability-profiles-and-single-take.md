# ADR-0017: Video-model capability profiles and the single-take hero format

- Status: Accepted (2026-11-04)

## Context
Real runs (M11) showed that most failures came from asking the video model for things it renders
badly: light-only events, identical products and faces across separately generated scenes, small
props and precise placement. A reference ad the user showed (one continuous ~8 s take of physical
action ending on the product) worked because it played to the model's strengths: physics,
motion, slow motion, one take with no cross-scene consistency risk.

## Decision
1. **Capability profile per video model** in `config/models.toml`: strengths, weaknesses and
   `max_single_shot_s`. Each item is marked *[observed]* (our real runs) or *[expected]* (vendor
   docs or general experience). Kling v3 Pro: 15 s, from its verified duration enum. The profile is
   injected into `ideate` (design concepts around it), `select` (producibility is scored against it,
   with single-take and multi-scene concepts on the same scale) and `storyboard`.
2. **Single-take hero format**: a storyboard may be ONE scene lasting the whole ad, up to the
   model's single-take limit. Multi-scene ads keep the 1.5–8 s per-scene range. For one scene:
   purpose `product` and the product inside the take (teased early, hero at the end); the
   one-action lint applies to multi-scene storyboards only, because a single take is one causal
   chain by design. If the brief asks for one continuous shot, all three concepts are single takes.
3. Brand names and taglines never appear in the footage; they go on the end card (M8).

## Alternatives considered
- Hard-coding the reference concept: rejected. The agent must ideate. The format and the profile are
  generic and serve any brief.
- One global "AI video can/can't" list in the prompts (the old approach): replaced, because models
  differ (LTX veils a passing shadow; Kling ignores it).

## Consequences
+ Concepts are designed for the model; the single take removes cross-scene drift and costs one
generation. − Profiles need upkeep per model, and *[expected]* items are unverified until tested.
− Under the hard/soft QA calibration, a single take whose key motion beat is weak still passes
(motion is not a hard check). See the M11 single-take result in PROGRESS.md.

## Findings from the first real single take (M11, run_01M6JH38ZT2WRDPSQ2GM833F7T)
- **Cheaper and fully consistent.** One keyframe and one 10 s clip: $1.425 on fal, about 11 min, no
  recoveries. The product looked identical for the whole take. The 4-scene majlis ad cost $2.925,
  delivered 3 of 4 scenes, and its bottle drifted between scenes.
- **The judge played it safe.** It chose the calmest, most producible concept (silk corridor:
  producibility 8, insight 5) over the one with a real human truth ("the friend who always borrows
  your perfume": insight 9, producibility 4 because of face and hands). Producibility currently
  outweighs insight whenever a concept touches a known model weakness.
- **A missing key action passed as "soft".** The promised beat (the bottle flying toward the camera
  and filling the frame) did not happen. QA passed the clip at 0.72 because under the hard/soft
  calibration (ADR-0016 addendum 3) a missing motion beat is not a hard failure. For a single take,
  where one action is the whole ad, "key action delivered" is a candidate hard check.
- The *[expected]* strength "objects flying toward or past the lens" stays unverified.

Open follow-ups (not built): a hard "key action delivered" check for single takes; rebalancing
the judge so a high-insight concept is not discarded for a moderate producibility risk.
