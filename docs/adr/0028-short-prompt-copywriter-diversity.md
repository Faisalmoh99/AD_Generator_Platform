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
