# ADR-0024: The creative layer: insight first, concepts with a story and a hook, rubric selection

- Status: Accepted (2026-11-06)

## Context
Interview feedback: "the system is an engineering proof, but there's no creativity in the video".
The judge (ADR-0014, ADR-0017) weighted *producibility* at 0.20 and played it safe: real runs
picked the calmest concept over the one with a real human truth. Concepts had a free-text
insight and a "visual hook", but no story structure, no stated role for the product, and culture
often showed up as props. The user decided: creative stages before the storyboard (Insight →
Concept with a story → Hook → selection by a creative rubric), six criteria (insight, hook,
story/turn, originality, culture as meaning, product's role), ease of execution removed from the
weights, the same rubric for the finished video, and the hook folded into ideate.

## Decision
1. **Insight stage** (`Stage.INSIGHT`, node `insight`, `insight` v1, `LLMNode.INSIGHT`): the
   category's clichés first, then 3–5 human truths, each with its tension, real-life evidence and
   what it means for a Saudi/Gulf viewer. Stored as `RunState.insights`.
2. **Concepts on the truths** (`ideate` v6, `CreativeConceptSetDraft`): each concept names the
   insight it stands on (checked by code), a story (setup / turn / payoff), a hook (image, sound,
   and the question it opens in the first two seconds), the product's role (hero | catalyst |
   reward | witness + why), culture as meaning, an originality claim and a 3–6 line shot sketch.
   The LLM schema has no optional fields beyond two lists (Anthropic rejected a schema with many
   nullable fields before). Stored `Concept`s keep the old fields (derived from the new ones) and
   add the creative fields as optional, so old runs load and old consumers keep working.
3. **The rubric as data** (`config/rubric.toml`, `domain/rubric.py`): six criteria with weights
   insight .20, hook .20, story_turn .15, originality .15, cultural_meaning .15, product_role .15;
   the cliché penalty (1.5) stays. Each criterion has a question for a concept and one for a
   finished video, so the whole-ad critic (Phase D) scores on the same scale.
4. **Selection** (`select` v6): the judge scores the six criteria; code weights them, applies the
   penalty and picks the winner (ties: insight, then hook, then the lowest number). The video
   model's profile is no longer shown to the judge; it still shapes ideate and the storyboard,
   and the critic's hard gate catches what fails on screen.

## Alternatives considered
- A separate HOOK stage (two alternatives per concept): one more call per run for a field the
  concept already carries; the user chose to fold it into ideate.
- Keeping producibility at a lower weight: it is how the judge played safe; production risk is
  better handled where it can be acted on (format grammar, storyboard, gates).
- Free-text rubric in the prompt: weights in code make every pick reproducible and testable.

## Consequences
+ The trace shows the reasoning in creative terms: the truths, the turn, the hook's question, the
  product's role, six scores and the weighted pick.
+ Old runs, the montage and narrative paths and the UI keep working (optional fields, same events).
− One more LLM call per run (insight, Sonnet at high effort).
− A creative score is a judgement, not a measurement; it is calibrated on real outputs (Phase D:
  the critic on montage v2 must score low on story, insight and hook).
