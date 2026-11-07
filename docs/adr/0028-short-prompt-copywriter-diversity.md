# ADR-0028: A short Seedance prompt with the turn first, a physics library, an Arabic copywriter and planned concept diversity

- Status: Accepted (2026-11-07). Part 1 (prompt) built in P2; parts 2-3 in P3-P4 (addenda).

## Context
The employer's feedback on the first demo was "an engineering proof, no creativity", and every
real ad so far needed a human for its Seedance prompt, its brand and its tagline. Research
(2026-11-07, plan file) found three things. Community Seedance 2.0 guides converge on 60-100 words
with the non-negotiables first ("8 requirements written, 4-5 honored, at random"), "Cut to"
cues between shots, "go small and slow", and naming what carries an object's weight; our stored
prompts were 411-509 words. Higgsfield's creative layer is templates, hooks and reference ads,
with no idea stage. Diversity research says planned spread beats more samples.

## Decision (part 1: the prompt)
1. **Layout by code** (`render_ad_prompt`): the product's identity is line 1, the key moment
   line 2 ("Key moment, shot 3, slowly over 2 s: <the key shot's event>."), then the header,
   References (props, compact when long; "No text, logos, subtitles or watermark."), Look, the
   shots ("Cut to:" before shots 2..n; the size sentence written from `product_in_frame`), Audio.
   The human-prompt parser accepts "Cut to:".
2. **Word budget** (`[params]` in `creative_rules.toml`, the user's rule): the writer's own words
   (look, every shot's action, camera and sound, audio) are at most 180; look 30, shot 35, audio
   20; and the identity and key-moment lines end within the first 30 words of the rendered text.
   Checked by code for the writer and the director (→ repair).
   **Deviation, stated:** the plan said "≤ 180 words" for the whole prompt. Measured on the
   fixture, the code-written lines alone (identity, key moment, header, References with two
   props, labels, timecodes) take ~100 words for four shots, so a whole-prompt cap would leave
   ~15 words a shot. The cap applies to the writer's words; the stored prompts had 281-351 of them.
3. **Seedance failure library** (`[[physics]]`, checked by code): `contact` (a held object names
   what carries its weight), `visibility` (a spray, steam, smoke or pour names its visible shape),
   `transformation` (a morph unfolds in visible steps). A product a hand uses is at least half the
   frame. Banned words gain "fast/quick/snaps/sudden" and "macro/extreme close-up" in the camera.
   Judge rule `physics_anchored` (12 rules). `[[templates]]` carry the measured Halai and
   Valentino numbers; `camera_moves` lists one-move shots. Writer v5 (with try 2 compressed as the
   exemplar), director v4.
4. **The shot count is picked by code from the length** (`whole_ad_shot_count`): every purpose
   with a minimum (turn 2 s, hero 2 s) at that minimum, the others at 1.5 s, as many shots as fit:
   6 s → 3 shots (hook, turn, hero), 8 s → 4. The storyboard is sent back otherwise.

## Alternatives considered
- A whole-prompt 180-word cap: forces skeletal shots for a 4-shot ad (measured); rejected.
- Leaving length to the director's judgement: it never shortened (all five runs > 400 words).
- Dropping per-shot timecodes (Morphic: timing is a hint): the critic and the human-prompt
  parser use them, and the key-moment line already states the turn's length.

## Consequences
+ `scripts/prompt_audit.py` shows every stored prompt failing the new budget and 4 of 5 tripping
  a physics rule (the floating hold, the invisible spray), so the checks target real failures.
− More repair rounds are possible on a real writer; the director has the exact counts.

## Addendum (part 2, P3): the Arabic copywriter stage

- `Stage.COPY` between PROPS and ESTIMATE on whole-ad runs (`agent/nodes/copywriter.py`), so the
  end card is shown at the spend gate. Two LLM calls, code decides: `copywriter` v1 (Sonnet high
  on balanced, Opus on quality) writes five taglines (≤ 5 words; device: proverb echo, contrast,
  pun, rhyme or plain; the saying it echoes; an English gloss; what part of the truth it says) and
  three fictional brand names when the brief has none. It is given the chosen concept's insight,
  its culture anchor (Arabic text, meaning, pitfalls) and the bank's proverbs and phrases for the
  category, with try 2's human line as the device to study, never to copy.
- **Code checks every line** (`domain/copy.py`, → repair): word count, Arabic script only for
  Arabic, no digits, quotes, hashtags or emoji, no empty luxury words, claims or religious words
  (`config/copy_rubric.toml`, matched on normalised Arabic with clitics), no real brand
  (`real_brand_in`), five different lines, brands only when the brief has none.
- `copy_judge` v1 (Sonnet, the user's choice) scores insight_tie .30, memorability .25,
  dialect_fit .15, sayability .15, brand_fit .15 and flags generic lines (−1.5); **code weights
  and picks** (ties: insight_tie, then the shorter line). Brands: the highest score.
- **Dialect by code** (`copy_dialect`, the user's rule): white Saudi, Najdi-leaning, by default;
  Hijazi when the request names Jeddah, Makkah, Madinah or the Hijaz; English for English.
- Stored as `RunState.copy_selection` (every line, its scores and the pick; a hash so the same
  concept is never written twice) and written to the end card (`SetEndCard`; the invented brand is
  marked a placeholder, the tagline is the system's proposal). A brand or tagline in the brief, or
  set with `--endcard-brand` / `--endcard-tagline`, always wins; with both given the stage makes no
  call. At assembly the system's copy fills only what the human did not give.
- Shown in the pack ("End-card copy" table), the decisions report and the studio's whole-ad panel.

## Addendum (part 3, P4): planned concept diversity and the motif ledger

- **Axes by code** (`assign_axes`): hospitality, family, self, occasion, travel, craft, season,
  no_anchor. The themes the insights stand on come first (hospitality only when an insight is
  anchored there), then a fixed fill order, one axis per candidate.
- **Six candidates on a whole-ad run** (`ADSMITH_IDEATE_CANDIDATES`, default 6; other formats
  keep three): ideate v8 writes one concept per axis with 2-4 English motifs (who, where, what
  object) and sees the recent winners as "recently used, avoid". Code checks the count and that
  each axis is used once (→ repair).
- **Code keeps three** (`pick_distinct`): concepts that share 2+ motifs with one of the last 10
  winners go last; then greedy max-min dissimilarity on the concept text. The select judge sees
  three, as before; a concept that still reuses recent motifs loses `novelty_penalty = 1.0`
  (rubric.toml), applied by code.
- **The ledger** (`motifs` table, migration 2; `storage/motifs.py`): one row per run, written by
  the orchestrator after the storyboard is committed on the winner (real-LLM runs only, so demo
  fixtures never count). Seeded with the five planning runs (`scripts/seed_motifs.py`): four
  hospitality winners at a door or a cup, one grandmother with a gift box.
- **Culture bank themes**: every entry has a `theme`; the insight stage gets a sample taken
  round-robin by theme (at most 3 per theme while others remain). Two entries added for coffee
  outside hospitality (`road_thermos`, travel; `home_roast`, craft), marked for the user's review.
- Research basis: planned spread over semantic directions beats more samples of one prompt;
  structures that pull ideas toward consensus collapse diversity (ACL 2026 Findings).

## Addendum: serving Saudi qahwa, dropped candidates kept, rewrite in place (2026-11-07)

- The coffee run `run_01M6TBP6H8VTXWGXFJR6XPVQ7M` had the father pour qahwa into a finjan from a
  glass brewer. As a general rule (the user's): Saudi qahwa is brewed in and poured from a
  dallah only; a glass brewer or pour-over belongs to dark specialty coffee in ordinary cups; the
  dallah is held in the left hand and the finjan offered with the right; the younger usually
  serves the elder, so an elder pouring for a younger person is an honour.
  - **Code check** (`[serving]` in `creative_rules.toml`, `serving_problems`): a shot that pours
    or serves into a finjan names the dallah and no brewer, carafe, V60, kettle or teapot; a
    prompt with a finjan has no such prop.
  - **Judged rule** `serving_correct` (14 rules) for the director.
  - **Culture bank**: `coffee_right_hand` corrected (it said "poured and served with the right
    hand"; the dallah is in the left), serving pitfalls on the coffee entries, the dallah's
    `not_like` names the glass brewer.
- `ConceptSet.candidates_dropped` stores the candidates code did not keep, in full (pack and
  decisions report list them).
- `adsmith rewrite-prompt <run> --reason`: a human sends a whole-ad run back to PROMPT before any
  money is spent on it (gate, estimate or a finished run with no attempt); the writer and the
  director run again; the concept, copy and end card are kept; generated and library props are
  cleared so PROPS follows the new prompt. A paid generation is refused (use `regenerate-ad`).
