# ADR-0021: Macro crops from the packshot, vocabulary fallback, generated music

Date: 2026-11-05 · Status: accepted

## Context

STOP 1 and its re-roll left three gaps before the first fully automatic montage:

1. **Macro identity.** Kontext *direct* keeps the packshot's framing (no true macro); Kontext
   *multi* (composite) returned the studio packshot itself. Both lose the cap or the glass edge.
2. **Hand re-rolls.** A shot whose keyframe failed the gate on every try (the water ribbon) was
   rescued by hand, which a fully automatic run must not need.
3. **Music.** Beat-cut assembly (ADR-0020) needs a track; the user wants none downloaded.

## Decision

1. **Identity mode `crop`.** A vocabulary entry may declare `identity_mode = "crop"` with
   `crop = "cap" | "edge"`. The scene's keyframe is cut from the approved packshot in code
   (`media/crop.py`: the product is the set of pixels that differ from the per-row border colour;
   the cap crop is ~1.7× the cap's width at the frame's aspect ratio; the edge crop is the upper
   right shoulder), upscaled to the frame and stored in the CAS. Kontext then only relights it
   onto black (`keyframe_relight` v1). The request's input image is the crop's sha256, so a new
   packshot is a new cache key. `cap_macro` and `glass_edge_macro` use it; `cap_lift` becomes a
   direct product shot with a reference note. The keyframe gate is told a macro shows only part
   of the product.
2. **Vocabulary fallback.** When a vocab shot's keyframe is rejected on its last allowed try, the
   scene runner swaps it to the entry's `fallback` (spec, flags, a fresh prompt from the prompt
   writer; new scene version, keyframe tries start again) and logs a `vocab_fallback` decision.
   Once per shot; the chain skips entries other shots already use and a second person shot.
3. **Generated music.** `fal.audio` in the catalog (Lyria 2: $0.10 per 30 s track, verified on
   the page, the schema and the pricing API). The orchestrator calls `agent/music.py` right before
   the assemble node of a montage run without a user track: cache → budget reserve (category
   `audio`, hard cap `music_max_usd` = $0.15) → one call with transient retries → CAS + run state
   (`RunState.music`, reused on re-assembly). Any failure is a decision event and the ad is
   assembled without music. Not a new stage: the transition table is unchanged.

## Alternatives considered

- Macro: a better multi-image editor, or text-to-image macros swapped afterwards. Both still let
  a model redraw the product; the crop cannot drift. Cost: macro resolution is bounded by the
  packshot's (Kontext's relight re-renders at its own ~1 MP, which sharpens it).
- Fallback: escalation ladder (MORE_TIME 15) with prompt simplification first. More moving parts;
  the vocabulary already names a safer neighbour for every entry.
- Music: Stable Audio 2.5 ($0.20, over the cap), ElevenLabs music ($0.60, per started minute),
  MiniMax v2 (lyrics required), CassetteAI / ACE-Step (cheaper, weaker for a cinematic score).

## Consequences

- Macro shots cost one Kontext call ($0.04) and their identity is the packshot's by construction.
  A lens or action edit on a crop shot keeps its still (cache hit): only the clip is redone.
- A fallback adds one prompt-writer call and up to `keyframe_max_tries` stills; a shot that
  already fell back fails as before (`done_partial`).
- Montage runs spend up to $0.10 on music unless `ADSMITH_GENERATE_MUSIC=false` or a user track is
  set; Lyria renders 30 s, assembly trims and fades it.
- The fallback scene's `description` becomes the entry's English label (not the request
  language): a known deviation from rule 9 for this internal field.
