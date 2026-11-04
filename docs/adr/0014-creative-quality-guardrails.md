# ADR-0014: Creative-quality guardrails (clichés, originality, real-run-driven fixes)

- Status: Accepted (2026-11-04)

## Context
The first real Arabic perfume run picked a concept that the judge *itself* described as a
category cliché ("the visible scent trail"). Schema validity says nothing about whether an ad is
worth making. We need guardrails for creative quality that are explicit, traceable and testable,
and a habit of checking real outputs, not just green tests.

## Decision
1. **Name the clichés before ideating.** `ConceptSetDraft.category_cliches` (3–5, user's
   language) is the **first** schema field. Structured output is generated in schema order, so the
   model commits to the list before writing concepts that must avoid it. The list is stored on
   `ConceptSet`, shown in the trace and the Production Pack, and passed to the judge. The prompt
   gives **no** example clichés: an earlier version's illustrative example was copied into a
   concept verbatim, so instruction examples come from an unrelated category or are omitted.
2. **Judge rubric with originality and a self-consistency penalty.** Weights: brief fit 0.25,
   **originality vs. category clichés 0.25**, producibility 0.20, product hero 0.15, cultural fit
   0.15. The judge also sets `uses_category_cliche`. Code subtracts a fixed **1.5** points when
   it's true, so a concept the judge flags cannot win on its other strengths. Ties go to brief fit,
   then originality, then the lowest number. Scores and the penalty are in the decision event.
3. **Effort is a quality lever, measured.** ideate stays at `high`: medium and low were 2.3× faster
   but regressed to the clichés (ADR-0007 has the numbers).
4. **Real-run review is part of the milestone.** Three real planning runs (Arabic perfume, English
   café, Saudi-dialect delivery app) exposed defects no unit test could:
   - `ProductSpec` was perfume-shaped (`bottle_shape`, `cap`, `liquid_color`). The café run put a
     cup in `cap`, and the app run wrote `bottle_shape: "Not a bottle: a modern smartphone"`. It is
     now category-neutral (`form`, `materials`, `colors`, `branding_*`).
   - Cultural over-correction: "modest, Gulf norms" became **"women"** in the bible's forbidden list
     and in negative prompts. Prompts now state that modesty concerns wardrobe, behaviour and
     context and never excludes groups of people. The rerun delivery ad has a young Saudi woman as
     its protagonist, and negatives use the neutral "additional people".
   - Length limits were arbitrary: 4 of 5 repairs across the runs were `maxLength` hits on DP-level
     lighting/composition text. The limits are relaxed (300 → 500 for spec text, 160 → 240 for
     story beats).
   - Bible-term flag detection was noisy (see the ADR-0010 addendum).
   After the fixes, all three runs completed with **0 repairs and 0 flag overrides**.

## Consequences
- + Cliché avoidance is visible end-to-end (list → concepts → judge flag → penalty → winner).
- + Each fix is pinned by a regression test, or by the fixture set harvested from a real run
  (`scripts/harvest_fixtures.py` → `perfume_ar`).
- − The cliché list is self-generated, so it shares the model's blind spots. A lexical blind spot
  also remains (ADR-0009). Mitigation: the judge is a separate call with its own rubric.
- − A fixed penalty is a blunt instrument. It's tunable in `domain/concept.py` and covered by tests.
