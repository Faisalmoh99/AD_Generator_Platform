# ADR-0026: Creative rules as data, structured references, and a creative director before the spend gate

- Status: Accepted (2026-11-07)

## Context
Two real whole-ad generations (try 1 `run_01M6RTDPZEDAG98QZ9KPXT4NRY`, try 2
`run_01M6RYP42HB0GBFFYBAY09SGCW`) needed a human to rewrite the Seedance prompt both times. The
human fixed the same things each time: a References block with each image's role and shot range,
a prop image for the burner, one location, the product handled by a person, a longer key moment,
hard cuts and no ambiguous nouns. A frame-by-frame analysis of a competitor's product-review ad
(Halai) and of a Seedance reference (Valentino) showed the execution gap in numbers: their
product is on screen 93–100 % of the time (54 % in a person's hands for Halai); ours was on screen
13–19 %. Our idea layer (insight, turn, culture as meaning) is not the problem; the prompt that
reaches the video model is, and no stage checks it against what we learned. The user decided the
system must write that prompt itself.

## Decision
1. **Rules as data** (`config/creative_rules.toml`, `domain/creative_rules.py`,
   `Settings.creative_rules`). `[params]` (minimum shot, key-shot and hero lengths, product shots
   and share of the runtime, the product itself in 2 shots, handled in 1, one location, at most 3
   references) and `[[banned]]` words (each with why and the replacement) are **checked by code**
   on every ad prompt; a failure goes back to the model through the schema-repair loop with the
   replacement named. `[[judge]]` rules (turn withheld, the product causes the turn, one location,
   a hook with motion, sizes named, no cliché, reference roles, a sound hit on the key event) are
   judged by the creative director (decision 4) by id. `[lessons]` are tries 1–2 and the two
   references, verbatim. The judge and lesson prose is data rendered by `prompt_block()`, the same
   precedent as `rubric.toml`, `culture.toml` and the `look`/`events` strings of `formats.toml`;
   the templates themselves stay in `prompts/` (CLAUDE.md rule 5).
2. **The user's screen-time rule** (2026-11-07): the product, or a prop declared
   `transforms_into_product`, is visible in ≥ 3 of 4 shots and ≥ 50 % of the planned runtime; the
   product itself in ≥ 2 shots, handled by a person in ≥ 1, and holds the hero ≥ 2 s at half the
   frame or more. What the turn withholds is what the product does, never the product.
3. **Structured prompt, References by code.** `ad_prompt_writer` v2 fills `location`, one
   film-level `lighting`, `references` (name, one physical line, role prop|location, shots,
   `transforms_into_product`), per shot an `event`, `shows_product`, `product_in_frame` and
   `handled`, and the `key_shot`. Code renders the human prompt's structure: header ("film", not
   "product film"), a References line (Image 1 is always the product photo; a reference gets
   "Image k" only when an image with its name exists, so numbering never drifts and a dropped
   image only loses its number), Look = location + look + lighting, numbered shots with a
   "Framing:" phrase for the product's size, Audio + "No voice, no dialogue." Shots may not name
   an image number. The writer now sees the product photo. Stored `AdShot`/`AdPrompt` are split
   from the LLM drafts and every new field has a default, so prompts stored before v2 (with
   per-shot lighting) still load under `extra="forbid"`.
4. **The multishot grammar** gains `min_product_shots = 3` (on `shows_product`) and
   `purpose_min_s = {reveal = 2, product = 2}`. The existing `min_product_share` (person-free
   shots) and `max_settings` (text only) are not used: one location and "handled" are prompt-level
   fields, so the storyboard schema and the other formats are unchanged. `storyboard` v9 changes
   only its multishot block.

## Alternatives considered
- Rules in the prompt text only: the model breaks them silently (tries 1–2); code checks with a
  named replacement make the repair loop fix them.
- A storyboard-level `handled` flag and location field: changes `StoryboardDraft` and every
  scenario's fixtures for a fact that only the whole-ad prompt needs.
- Rendering "Image 2" from the writer's text: the numbering depends on which images exist after
  the props stage; code is the only place that knows.

## Consequences
+ The prompt that reaches the gate has the structure a human had to write by hand.
+ Each rule is a line of data with a test; a rule that is wrong is changed in one place.
− More fields for the writer to fill: a larger schema and slightly longer prompts (the fixture
  renders at ≈ 1950 of 2500 characters).
− A banned word in an otherwise good prompt costs a repair round.
