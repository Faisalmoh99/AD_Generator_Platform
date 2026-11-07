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

## Addendum (2026-11-07): the creative-director stage (phase 2)

- `Stage.DIRECTOR` sits between PROMPT and ESTIMATE on whole-ad runs (`_after_prompt`); a pure
  node (`agent/nodes/director.py`, template `director` v1, `LLMNode.DIRECTOR`: Opus at high effort
  on balanced and quality, Sonnet low on fast; the user's choice). It sees the product photo, the
  concept, the storyboard, the bible, the format, every rule (code-checked and judged), the
  lessons and the run's clichés, the rendered prompt and its structured draft.
- Output: one verdict per judged rule id (code checks the set), changes and a summary in the
  user's language, and a full rewrite. The rewrite is checked by code against its OWN timings
  (same shot count, each shot within the format's length range and its purpose's minimum, ending
  at the ad's length), every creative rule and the prompt limit; failures go to the repair loop.
- **Code decides what is kept** (CLAUDE.md rule 1): all verdicts pass → the writer's prompt stays
  and the rewrite is only logged; a rule failed → round 2 (the round-1 verdicts and rewrite as
  input, "confirm or fix") and the rewrite replaces the writer's prompt (`template_version =
  director@v1`); round 2 invalid → the round-1 rewrite is used, with a note; round 1 invalid after
  repairs → the writer's prompt stays and a warn decision is logged. The run never blocks here.
- Re-timing within the grammar is allowed (the user's choice); `SyncStoryboardDurations` makes the
  storyboard follow, and the critic's windows already follow the prompt's shots.
- `RunState.director_review` (default None) stores the rounds, the outcome, before/after and a
  `prompt_hash`; the same prompt is never reviewed twice (a resume or a reset to the stage costs
  nothing). A human's prompt (`approve --prompt-file`) goes straight to ESTIMATE and is never
  reviewed. Edits that reset the run to PROMPT write a new prompt, which is reviewed.
- The studio shows a Director lane and, in the whole-ad panel, the verdicts per rule, the changes
  and the writer's prompt when it was replaced; the production pack has a "Director review"
  section and is also written after DIRECTOR.

## Addendum (2026-11-07): prop reference images (phase 3)

- `Stage.PROPS` runs after DIRECTOR and before ESTIMATE on whole-ad runs. It is an ad stage
  (`AD_STAGES`), routed by `_ad_step` to `agent/props.py` `PropsRunner`, which commits its own
  atomic updates like the generation runner; `AdGenerationRunner.run` now refuses a stage it does
  not run instead of silently advancing.
- For each prop reference of the prompt (at most 3) that no reference image covers by name (a
  user `--ref-image` wins), a square 768 px text-to-image on the default image model
  (`flux_dev`, $0.025; FLUX schnell is not in the catalog and was not added), template
  `prop_reference` v1. Budget: reserved, then settled or released, as the product reference;
  `ADSMITH_PROPS_MAX_USD` (default $0.20) refuses before the call. Cache by prompt, size and seed.
- Vision check `qa_prop_ref` v1 (Image 0 the product photo, Image 1 the prop): the described
  object, wordless, alcohol-free, brand-safe, and not resembling the product (a "product" issue,
  hard by `gate_draft`). A prop that transforms into the product may share one material, never
  the shape. One retry with the critic's hint; then the prop is dropped with a warning and keeps
  its description in the References line without an image number.
- Accepted images join `reference_images` (`PropReference.source = "generated"`, `cost_usd`,
  `check`; defaults keep older runs loadable) after the user's, and the prompt's text is
  re-rendered so the References line numbers them. Today only the writer checked the prompt
  limit; the re-render checks it too and drops generated props last-first until the text fits.
- The director's "already reviewed" check now hashes the prompt's content, not its text, so the
  renumbering does not count as a new prompt.
- The gate shows every image in order with its source, cost and check (CLI, studio thumbnails,
  production pack, written after PROPS too). Zero-key runs make mock props.

## Addendum (2026-11-07): the Saudi culture bank (phase 4)

- `config/culture_bank.toml` (32 entries, to be reviewed by the user before the first real run):
  rituals, proverbs, occasions, gestures, objects and phrases, each with its Arabic text (logical
  order), its meaning, how an ad can stand on it, the product categories it fits and its
  pitfalls; `[aliases]` maps English and Arabic words to categories (perfume, incense, coffee,
  dates, fashion, food). `domain/culture_bank.py`, `Settings.culture_bank`.
- The brief's category is matched in its English category and product and in the raw request;
  Arabic aliases match only at a word start (with و/ف/ب/ل/ك and ال), because «عود» (oud) occurs
  inside «سعودية» (Saudi) and turned a coffee brief into a perfume one (caught by a test).
- `insight` v2 receives up to 12 entries (the brief's categories first, then those for any
  product) and names, per insight, the entry it stands on (`culture_anchor`) or "none"; code
  refuses an id it was not given (→ repair). Stored `Insight` has `culture_anchor = "none"` by
  default, so earlier runs load. Ideate sees the anchor's Arabic text, meaning and pitfalls under
  each insight with no template change; the pack lists the anchors.
