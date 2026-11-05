# ADR-0019: Per-shot identity routing and a cultural wardrobe lexicon

- Status: Accepted (2026-11-05)

## Context
Two hand fixes from the hero rounds remained: choosing direct vs composite Kontext for each shot
(scale: the packshot dominates in direct mode, ADR-0003 addendum 4), and patching the bible's
wardrobe when "mandarin collar + button placket" produced a South Asian kurta instead of a Saudi
thobe (HANDOFF lesson 9).

## Decision
1. **Identity mode per shot, by code.** `identity_for(settings, scene)`: the shot vocabulary
   entry's `identity_mode` (ADR-0018) when the scene has one; otherwise the legacy global
   `ADSMITH_PRODUCT_REFERENCE_MODE` for product scenes (narrative runs). `MediaGateway.prepare`
   takes the mode: none → plain text-to-image; direct → Kontext from the packshot; composite →
   plain keyframe then Kontext multi. Both providers are always wired when a reference exists.
2. **The locked reference is on by default** for formats whose grammar says
   `identity = "reference"` (montage, single take) via `reference_enabled`; `--product-ref` still
   forces it for narrative runs. `RunConfig` is unchanged.
3. **Framing-aware wrappers**: `keyframe_reference` v2 tells Kontext to re-frame the packshot for
   a macro entry; `keyframe_composite` v2 places the product by the entry's framing.
4. **Trace**: every keyframe attempt event carries `identity` and `vocab`, so the STOP 1 report is
   read from the trace.
5. **Wardrobe lexicon as data** (`config/culture.toml`): how to describe Saudi dress, the
   look-alikes to avoid (kurta, kandura, dishdasha), and regex lints. It applies to Arabic briefs
   or briefs that mention the Gulf. The lint runs on the bible's character in the continuity node
   (→ schema repair; negated clauses ignored); the text goes into continuity v4, prompt_writer v8
   (person shots only) and qa_keyframe v6, where a look-alike garment is a major "character"
   issue (hard, through continuity_ok).

## Alternatives considered
- Keep one global mode per run: rejected, a montage mixes macro, hero and hand shots.
- A vision check for garments only at the gate: the gate already misreads; fixing the bible text
  first is cheaper and deterministic.

## Consequences
+ The direct/composite choice and the thobe patch are no longer manual.
+ The Arabic scripted fixture's own bible had "mandarin collar"; it was corrected (the lint
  caught exactly the Step A phrasing).
− Cache keys of product keyframes change (new wrapper prompts); stored runs regenerate if re-run.
− `prompt_rewrite` does not get the lexicon yet (the gate's issues carry the fix).
