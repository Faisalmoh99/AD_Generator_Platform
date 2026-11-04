# ADR-0018: Ad formats as data, one format per run, and a shot vocabulary

- Status: Accepted (2026-11-05)

## Context
The hero rounds got their quality from hand fixes: picking a structure around what Kling renders
well, re-speccing failing shots, reframing people as details, rewording specs to dodge the
flag-matching heuristic. The two montage references (PLAN_V2) show the target: 8-14 one-second
shots of physical events around a still product, one dark rim-lit look, the same bottle everywhere.

## Decision
1. **Formats are data** (`config/formats.toml`): `montage`, `single_take`, `narrative`, each a
   `FormatGrammar` (shot count, kept seconds per shot, hero last, person cap/framing/position,
   product share, allowed camera moves, look, events, risk notes). Code reads the grammar only.
2. **One format per run, resolved by code** (`resolve_format`): the request names one → that; one
   requested shot → single take; a requested count below the montage minimum → narrative; a
   physical product → montage; else narrative. All three concepts are written in that format.
   (The approved plan had "≥ 2 montage concepts, the judge picks"; changed at review because the
   judge has played it safe before and a non-montage pick would make the proof run meaningless.)
   `Brief.format` and `Storyboard.format` default to `narrative`, so stored runs load unchanged.
3. **Montage durations are kept seconds** (1.0-1.5 s). The model still renders its minimum (Kling
   3 s); `render_window` already separates rendered from kept time. `MIN_SCENE_S` is 1.0; narrative
   scenes keep the model minimum through `scene_duration_bounds`.
4. **Shot vocabulary** (`config/shot_vocabulary.toml`, 16 entries from the references): framing,
   `needs_product`, `shows_person`, `identity_mode`, `key_shot`, defaults for shot type, camera,
   speed, lighting, motion and a keyframe hint, plus a `fallback` (for MORE_TIME 15). Every montage
   shot names one (`Scene.vocab`, part of the creative spec and its hash).
5. **Flags come from the vocabulary.** For a scene with `vocab`, `shows_character` /
   `shows_product` are set from the entry and `enforce_scene_flags` skips it. This is an exception
   to ADR-0010's text-based upgrade, justified because the entry is a declared fact rather than an
   LLM guess; it removes the "spray"/"perfume" false positives. A person described in a
   person-free entry is a repair, not an upgrade.
6. Validation is deterministic and feeds schema repair (`grammar_problems` + the person check).

## Alternatives considered
- A judge choosing between formats: rejected (see 2).
- A `fit` matrix per video endpoint for every entry: replaced by an optional `avoid_on` list.
- New stages or a montage-specific node: unnecessary; the storyboard node validates per format.

## Consequences
+ The structure, shot design and flags that were hand work are now rules and data.
+ Templates: understand v4, ideate v5, storyboard v6 → v7, prompt_writer v7, prompt_rewrite v7.
− 8-12 keyframes per ad: more gate calls and stills money; `max_total_attempts` raised to 80.
− The vocabulary needs upkeep as models change; `[observed]` evidence for entries comes from STOP 1.
