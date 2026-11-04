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
