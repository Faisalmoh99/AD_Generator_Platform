# ADR-0029: Visual devices, a visual criterion, and an execution path that keeps the idea

- Status: Accepted (2026-11-07). Part 1 (devices in ideate) built; parts 2-4 in addenda.

## Context
The employer wants "a creative layer on top of Higgsfield with Higgsfield-level results". Our
pipeline finds true human insights (culture bank → insight → ideate → rubric judge), but the
winners are ordinary domestic scenes: a man pours coffee, a woman sprays her wrist. Try 2's human
idea (`run_01M6RYP42HB0GBFFYBAY09SGCW`: the incense smoke condenses into the bottle) had a device
and read as an ad. Competitors (Halai, Valentino) have spectacle and no idea. Reading the code:
nothing in the concept schema asks for an image, none of the six rubric criteria is visual, the
six candidates spread over culture themes (who and where) but never over how the idea is shown,
and ideate v8 still told the model to avoid transformations (a leftover from the scene formats).

The first system-alone video (`run_01M6TBP6H8VTXWGXFJR6XPVQ7M`, critic creative 5.8) also showed
why ordinary images are fragile in generation: a socially meaningful but visually ordinary action
(an elder pours for the younger) renders as "a man pours coffee"; a visually anomalous device
cannot be rendered as anything generic.

Research: Goldenberg, Mazursky & Solomon (Marketing Science, 1999): ~89 % of 200 award-winning ads
fit a small set of creative templates (50 % of winners vs 2.5 % of non-winners), and novices
trained on templates beat brainstorming. LLM ideation homogenises; planned spread and personas
help, prompt or temperature tweaks do not (Doshi & Hauser; Moon et al.; Deng, Brucks & Toubia);
asking for many candidates first raises diversity (Verbalized Sampling, arXiv 2510.01171;
divergent-then-convergent prompting, arXiv 2512.23601).

## Decision (part 1: devices in ideate)
1. **The library as data** (`config/visual_devices.toml`, `domain/visual_devices.py`,
   `Settings.visual_devices`): 12 devices (make the invisible visible, a trail to follow,
   transformation, match on shape, time freeze, the room responds, reveal by reflection, one-take
   reveal, extreme consequence, product POV, scale shift, rewind), each with what the viewer sees,
   why only video can show it, its categories, the wording that renders it (tested against the
   banned words and the physics library), its renderability (proven only with the run that proved
   it) and the key shot's minimum length. Two worked exemplars: the user's boy who follows his
   father's scent, and try 2's burner-to-bottle.
2. **Ideate v9 + schema**: `CreativeConceptSetDraft.device_sketches` (raw one-liners written
   first, ignoring constraints) before the concepts; each concept has `device_id`, `device_image`
   (the one image a viewer remembers) and `device_shot` (the sketch line where it happens, the
   turn). The stored `Concept` has the three as optional fields, so old runs load.
3. **Code checks** (`ideate.device_problems`, → repair): the device fits the brief's category;
   the candidates use different devices; `device_shot` is within the sketch; no concept shares 2+
   motifs with an exemplar; on a whole-ad run the sketch has exactly the film's shot count
   (`whole_ad_shot_count`), so an idea is born at the size it will be filmed.
4. **Reuse across runs**: the motif ledger stores the winner's device (migration 3); a device one
   of the last three winners used counts as reuse in `pick_distinct` and is shown to ideate.
5. `concept_block` shows the device to the storyboard, the writer and the director.

## Alternatives considered
- Assigning (theme, device) pairs by code: forces poor matches; distinct devices across the
  candidates spreads them just as well and lets the model pick the device that fits each theme.
- A separate brainstorm call: one more call and a hand-off; the sketches as the first field of
  the same structured answer give the divergent step for free.
- Devices as free text: the model invents unrenderable spectacle; a library carries the wording
  that renders and what has been proven.

## Consequences
+ Every concept now names its image; the judge, the director and the critic can be asked about it.
+ Diversity spreads over how ideas are shown, not only who is in them.
− A larger ideate schema and more repair reasons; the multishot fixture gained a fourth shot line.
− "Likely" and "unproven" are our judgement until a generation proves them.
