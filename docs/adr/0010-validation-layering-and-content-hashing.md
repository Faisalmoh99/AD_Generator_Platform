# ADR-0010: Validation layering and scene content hashing

- Status: Accepted (2026-11-04)

## Context
Two foundational questions came up while implementing the domain:
1. Some invariants span objects. For example, the storyboard's durations must sum to the
   **brief's** `duration_s`, but `Storyboard` doesn't hold the `Brief`. Where does that check live?
2. Caching and blast radius both need to answer "did this scene's spec change?". What exactly goes
   into a scene's hash?

## Decision 1: validation layering
- **Draft vs. domain models.** Every LLM-facing schema is a `*Draft` (e.g. `ConceptDraft`,
  `SceneDraft`, `StoryboardDraft`, `QAVerdictDraft`, `ScenePatchDraft`) with no system fields
  (ids, status, versions). The domain model extends it and is built by a `from_draft(...)` factory
  that adds those fields. The LLM is never asked to invent IDs or lifecycle state.
- **Object-local invariants live on the object.** `Storyboard` **owns a copy** of
  `target_duration_s`, set from `brief.duration_s` in `Storyboard.from_draft`. "Durations sum to
  target ±0.25 s" is therefore self-contained and checked on every construction *and every load*.
  The error message ("sum to 14s (3.5 + 3.5 + 3.5 + 3.5) but the target is 15s…") is written to be
  fed straight back to the LLM by the repair loop.
- **Cross-aggregate invariants live on the aggregate root**, `RunState`: storyboard target ==
  brief duration, storyboard belongs to the selected concept, no prompt references a future bible
  version, and each stage has the data it needs. `RunState` validates on every snapshot load, so a
  corrupted or hand-edited snapshot fails at the boundary.
- `validate_assignment` is **off**. Multi-field changes go through explicit transition methods
  (`Scene.fail()`, `mark_stale()`…) that check legality against a transition table and re-check
  invariants once, atomically.

Rejected: **pydantic validation context** (`model_validate(..., context={"brief": ...})`). It is
invisible at call sites, easy to forget, and the snapshot loader would either need the context or
silently skip the check. A **separate validator service** would scatter rules away from the data.
Not copying the target into the storyboard would make `Storyboard` unvalidatable on its own.

## Decision 2: what a scene hash covers
`Scene.content_hash()` = `sha256("scene.content.v1" + canonical_json(spec))` where the spec is
exactly `SPEC_FIELDS`:

| Included (the creative spec) | Deliberately excluded | Why excluded |
|---|---|---|
| `purpose` | `id`, `order` | identity/position, not content (reordering ≠ regenerating) |
| `description` | `status`, `phase`, `stale_from`, `failure` | lifecycle, not content |
| `duration_s` | `attempts`, `version`, `history` | bookkeeping |
| `shot` (all 11 fields) | `prompt` | **derived** from spec + bible + template; hashing it would make the spec hash change whenever we re-prompt |
| `shows_character`, `shows_product` | `keyframe`, `clip`, `qa` | derived artifacts |
| | bible | run-level; handled explicitly by the blast radius per section (`uses_bible_sections()`), so a wardrobe change doesn't touch product-only inserts |

A second hash, `keyframe_spec_hash()`, is the spec **minus motion-only fields**
(`duration_s`, `shot.camera_movement`, `shot.speed`). Unchanged ⇒ the keyframe is still valid ⇒
invalidation depth `clip`. These constants (`MOTION_SCENE_FIELDS`, `MOTION_SHOT_FIELDS`) are the
same ones the blast-radius algorithm uses, so hashing and invalidation cannot disagree.

### The bible-dependency flags are biased toward `True`
`shows_character` / `shows_product` decide which scenes a bible change invalidates. They come from
the LLM, and the two errors are **not symmetric**:

| Error | Effect | Cost |
|---|---|---|
| False positive (flag true, element absent) | scene is regenerated after a bible edit it didn't need | one extra keyframe + clip (~$0.45) |
| False negative (flag false, element present) | scene keeps the **old** wardrobe/bottle after the bible changes | silent continuity bug in the final ad |

So we bias toward true at three layers:
1. Schema defaults are `True`, and the field descriptions (part of the prompt) say "when in doubt,
   mark true". The storyboard template repeats it (M4).
2. A deterministic cross-check (`domain/scene_flags.py`) **upgrades** a flag to `True` when the
   scene's description, action or composition mentions the element. It uses generic EN/AR terms
   (man/hand/he, شاب/يد/هو; perfume/bottle, عطر/زجاجة), with Arabic article and clitic handling.
   Once the bible exists it adds the bible's own terms: wardrobe items and the product name/label.
   It runs after `storyboard` (generic terms), after `continuity` (bible terms) and after every
   edit.
3. It **never downgrades**. Every override is emitted as a `decision` event with the matched terms,
   so the trace explains why a scene was later regenerated.
Known false positives (accepted): a wardrobe colour word ("black") can match a product-only scene
("black bottle"), and an Arabic prefix match can catch an unrelated word. Both cost a regeneration,
never continuity.

**Three layers of change detection, each with one job:**
1. `content_hash` / `keyframe_spec_hash`: *did the scene's spec change?* (blast radius, staleness)
2. `ContinuityBible.section_hash(section)`: *which bible sections changed?*
3. `GenerationRequest.cache_key()` = hash of provider, model, phase, final prompt text, params and
   input keyframe sha256: *has this exact generation been done?* (idempotency). Prompt-template
   or bible changes surface here because the final prompt text changes.

Canonicalisation: sorted keys, compact separators, UTF-8 preserved, all strings **NFC-normalised**
(so visually identical Arabic hashes the same), and `allow_nan=False`. Each hash has a **versioned
namespace**: changing what goes into a hash means bumping `v1 → v2`, which invalidates old entries
on purpose.

## Consequences
- + Every rule has one obvious home. Failures produce messages good enough for LLM repair.
- + A property test proves non-spec mutations (order, prompt, version bumps) never change the hash.
- − The storyboard's copied target can drift from the brief. That is exactly what the `RunState`
  check catches.
- − Adding a field to `Scene` requires deciding whether it's spec (add to `SPEC_FIELDS`, bump the
  namespace) or not. The table above is the reference.
