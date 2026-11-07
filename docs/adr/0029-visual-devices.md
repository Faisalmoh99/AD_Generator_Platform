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

## Addendum (part 2): visual_magic, a judge read in a shuffled order

- `config/rubric.toml` gains `visual_magic` (one image only a moving picture can show, new to the
  category, carrying the turn) at .20; insight .15, hook .15, story_turn .15, originality .10,
  cultural_meaning .10, product_role .15 (the user's weights). On the old six criteria the critic
  scored the human try 2 6.0 and the system's coffee film 5.8: a device was worth almost nothing.
- `ConceptScore` and the critic's `AdRubricScores` gain the score; a stored verdict's scores are
  `StoredAdRubricScores` (visual_magic optional), so verdicts made before load.
- Judge bias (position, length, style over substance: Zheng et al. 2023; Ye et al. ICLR 2025;
  LitBench, EACL 2026): `select` shows the concepts in an order shuffled by a seed from the run id
  (`judge_order`; the same order on resume) and maps every score back (`back_to_concepts`); each
  concept opens with its one image before the prose (`concept_block(image_first=True)`); select v7
  says length and polish are not quality. Scripted runs keep the given order unless
  `ADSMITH_SELECT_SHUFFLE_SCRIPTED` is on, so the demo fixtures keep their winners.
- The novelty penalty also applies to a recent winner's device (`is_reused`).
- `qa_ad_critic` v4 looks for the concept's device on screen: visible, at least half the frame,
  held for most of its shot, carrying the turn.

## Addendum (part 3): human preferences logged at the gates

- Fine-tuning, the honest answer for the employer: the Claude API offers no fine-tuning, and on
  small data preference tuning adds almost nothing to a generator (arXiv 2603.20100). What pays
  later is a judge trained on pairwise human picks (LitBench: 78 % agreement with people vs 73 %
  for the best prompted judge), which needs a thousand or more pairs. So we log now, cheaply.
- `storage/preferences.py` (`PreferenceLog`, table `preferences`, migration 4): one
  `PreferenceRecord` per human decision with every option, the system's scores, its pick, the
  human's pick or own text, and the context (request, concept, insight, device). Written after
  the command commits (a write failure is logged, never fails the command) by `choose_concept`,
  the new `choose_copy` (`adsmith choose-copy <run> --tagline N --brand N`: a human picks one of
  the copywriter's lines; the end card follows; code's own pick is kept on `CopySelection`),
  `set_endcard` (a human's own line beats all five), `approve_generation` / `decline_generation`
  (the prompt, and the writer's version when the director replaced it) and `accept_ad` /
  `regenerate_ad`.
- `scripts/export_preferences.py` writes (preferred, rejected) JSONL pairs and prints how often
  the human agreed with the system's pick: the first calibration number for every judge.
- The gate printout lists the five taglines and three brands with code's pick marked.

## Addendum (part 4): the execution path keeps the idea

What the coffee video lost, each now caught before the spend gate:
- **Both faces** (`ContinuityBible.second_character`, `continuity` v7): the bible described only
  the son, so the father was hands and a torso at his own turn. A second person whose face carries
  the turn is described like the first (wardrobe lint included); stored bibles load without it.
  `emotion_on_turn` now names whose face: the person whose feeling or gesture the insight is about.
- **Object fidelity** (culture-bank `[[objects]]`: `always_say`, `wrong_words`, `max_count`):
  code glues "handleless" before every finjan in the shots, refuses "cups / teacups / mugs /
  saucers" when a finjan is in the prompt (`object_problems`, writer and director → repair), and
  says "exactly one in the whole film" for the dallah (full and compact References lines).
- **Two beats in the key shot** (`key_max_beats = 2`, `key_beat_problems`, clauses split by ';' or
  a full stop): the coffee key shot had four in 2.5 s. Steps of one event stay one beat.
- **The device's length** (`device_length_problems`): the key shot lasts at least the concept's
  device minimum.
- **Judged rules** (17): `device_lands` (the device IS the key shot's event, most of it, half the
  frame, the turn depends on it), `setup_filmed` (shot 1 is the setup the turn answers),
  `no_holdup_opening` (the generic product-review opening the critic named); `hook_motion` accepts
  the product standing in frame when the hook is a gaze toward it, so `product_active`,
  `hook_motion` and the size rules no longer compose into the hold-up opening.
- **The device reaches the prompt** (`device_block`): `ad_prompt_writer` v6 and `director` v5 get
  the concept's device, its one image, its shot, its minimum length and the wording that renders
  it; the director keeps the device and the setup when two rules pull against each other.
- Ideas are born at the film's shot count (part 1), so the director no longer has to cut a beat.

Deferred: the production pack's shot list follows the storyboard, which a prompt rewrite does not
change (the coffee pack still lists the glass brewer); a storyboard sync after `rewrite-prompt`.

## Addendum (part 5): fixes from the perfume video and the planning runs (2026-11-07)

- **The device's length is set by the storyboard** (`32a57c7`): the writer must keep the
  storyboard's timings, so a 2.5 s device minimum checked on the writer was unsatisfiable; on a
  one-generation ad the reveal's minimum is now the device's (shot count, check, prompt line).
- **An unheld product names its support** (`[support]` in `creative_rules.toml`,
  `unsupported_shots`): a shot that shows the product with no hand on it names what it stands or
  lies on, with a preposition ("on the bench", "in her open palm"; "between palm trunks" is not a
  palm). Until it does, the shot is sent back and does not count toward the product-shots rule.
  The perfume video's shot 2 ("from the open bottle", added only to keep the product in three
  shots) rendered a bottle floating beside the mother. Lesson `try_8`.
- **A reunion ends on both people** (judged rule `reunion_payoff`, 18 rules; `qa_ad_critic` v5):
  the perfume payoff showed hands only and the critic said the reunion was never shown.
- **Four repair rounds for the writer and the director** (`Settings.max_repairs`,
  `ADSMITH_LLM_MAX_REPAIRS_HEAVY`, default 4; other nodes keep 2): the perfume writer converged
  but ran out of rounds 3 words over one shot. A word-budget overage of 10 % or less
  (`word_tolerance`) is a logged warning, not a failure; the identity-and-key-moment line stays
  strict (it is an order, not a budget).
- **The production pack follows the final prompt**: for a one-generation ad its shot list is the
  prompt as sent (after the director, a rewrite or a human prompt), never the storyboard's first
  plan; `final_shots` in the JSON; the pack is also written after ESTIMATE.
- **Where the Kie key comes from** (`secret_source`): `kie-balance` and the spend gate say "shell
  environment" or ".env" and warn when both are set and differ, never the value. The shell's key
  (an account at 9.8 credits) silently won over the topped-up `.env` key (80 credits).
