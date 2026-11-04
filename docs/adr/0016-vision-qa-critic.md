# ADR-0016: Vision QA critic

- Status: Accepted (2026-11-04)

## Context
The QA stage decides whether a paid clip goes into the ad or is regenerated. M5 shipped the loop
with a deterministic `MockCritic`; M6 adds the real vision critic. Requirements: the verdict must
be actionable (fixes written as prompt edits), cheap enough to run on every clip attempt,
internally consistent, and must never turn a rejection into a free re-serve of the same bad output.

## Decision
1. **Inputs**: the keyframe plus 3 frames at 15/50/85% of the *played* part of the clip
   (`min(scene duration, clip duration)`), each labelled in the message ("Image 1: clip frame at
   0.60s (15%)"). Models snap durations upwards and assembly trims the tail, so judging the tail
   would reject clips for frames nobody sees. Images are re-encoded to JPEG, ≤768 px long side
   (identity, product shape and artifacts are legible; vision tokens stay low). Also the shot
   spec, only the bible sections the scene uses, and the three prompts that produced it.
2. **The LLM fills fields; code decides** (ADR-0002): `passed` is computed (score ≥ threshold, no
   blocker, brand-safe). `frames_inspected` is set by code to the number of images actually sent.
3. **Deterministic consistency rules** run inside `convert`, so violations go through the normal
   schema-repair loop: every failed check needs an issue of a matching category (a check-to-category
   table), and every `frame_index` must name an image that was sent.
4. **Known-bad outputs leave the cache**: on rejection the rejected clip's cache entry is evicted,
   plus the keyframe's when the issue categories say the look is wrong. A keyframe retry uses
   `seed = attempt_no - 1`, so even a byte-identical prompt (a rewrite that changed nothing) yields
   a new image. Before this, a look rejection whose rewrite kept `keyframe_prompt` unchanged was
   "regenerated" from cache: the same rejected image, at $0, then rejected again. Found by the M6 tests.
5. **Selection**: `qa_mode=auto` → mock critic with mock media (placeholder frames would always
   fail), LLM critic with real media. `ADSMITH_QA_MODE=llm` forces it. Packaged scripted
   scenarios include a passing verdict so the LLM-critic path also runs with zero keys.

## Alternatives considered
- **Sending the whole clip / many frames**: costlier per attempt with little gain for 2–5 s shots.
  3 frames + keyframe catch identity drift, morphing and product loss.
- **Trusting the LLM's `passed`**: rejected (ADR-0002); thresholds stay in config.
- **Keeping seed 0 on every keyframe retry**: preserves strict determinism but makes a look
  rejection unrecoverable when the rewrite is a no-op.
- **Cross-scene reference image** (show the critic an approved neighbour to check "same man, same
  bottle"): deferred. Scenes run concurrently, so no stable reference exists at QA time; continuity
  is checked against the bible text and within the scene (keyframe vs. frames). Revisit after real
  output (M11 step 4).

## Consequences
- The critic cannot be meaningfully validated on mock frames; calibration happens on real fal
  output in M11 (a good clip must pass, a product-removed variant must be rejected with a
  specific reason).
- A rejected keyframe retry is a real generation (cost), which is the point.
- Forks from a pre-keyframe snapshot still get seed 0 on attempt 1, so the cache demo is unchanged.

## Addendum (M11, after the first real clips)
1. **Stricter pass rule, in code**: pass ⇔ score ≥ threshold AND `product_visible` AND
   `continuity_ok` AND `brand_safe` AND fewer than 2 major issues AND no blocker
   (`QAVerdict.fail_reasons`). Reason: a real LTX clip passed at 0.72 with 2 major issues and 2 failed
   checks. Attempts store `qa_passed`, so high-score rejections count toward the regeneration
   limit (counting `qa_score < threshold` alone would loop).
2. **The rule is applied at decision time, not on load.** Tightening it made a stored approved
   snapshot fail its load invariant. The snapshot invariant now only requires a clip plus a
   verdict, and `approve()` enforces the current rule. History is not re-judged.
3. **Keyframe as its own image**: a non-minor, non-motion issue located in image 0 regenerates the
   keyframe, whatever its category (e.g. key light from the wrong side). The rewrite is told the
   code's decision, and a kept keyframe is enforced on its output.
4. **qa_critic v2**: an explicit keyframe checklist (key-light side vs. spec/bible, required negative
   space, unrequested objects, framing/product), and `product_visible` = clearly readable in
   every shown frame (veiled/hazed/occluded = false). Calibrated on identical real inputs
   against v1 (M11 log in PROGRESS.md).
5. Known limits seen on real output: the critic is not deterministic (the same clip scored 0.72,
   then 0.62, under v1), and v2 still missed product-identity drift (cap shape, engraved
   lettering on the collar) on a regenerated keyframe.

## Addendum 2 (M11): known limitations, measured on real output
- **Non-determinism.** The same critic and template on the same inputs scored the same LTX clip
  0.72 and then 0.62 (v1). v2 caught the wrong key-light side on one input and missed it on another
  with the identical keyframe, when a dominant clip defect (the veil) drew its attention. The pass
  rule is deterministic, but its inputs are not. Borderline verdicts can therefore flip between runs.
  *Mitigation ("more time", not built):* ask for a second opinion only when the score is within
  ±0.1 of the threshold or the verdict hangs on a single major issue, and decide by agreement
  (both must pass). It costs about $0.02–0.03 per borderline clip and nothing elsewhere.
- **Fine product identity.** v3's bible-derived checklist (split into atomic items) caught the engraved
  collar lettering on 2 of 2 runs, but missed a cylindrical cap that should be squared on 2 of 2
  runs. At 768 px in a three-quarter view a cylinder and a squared block have nearly the same
  outline. *Mitigation ("more time"):* give the critic a product reference image (an approved
  hero keyframe or a real product photo) to compare against, instead of text alone.

## Addendum 3 (M11, final): severity calibration (hard vs. soft)
**The calibration story, measured on real fal output:**
1. *Too lenient* (v1, "only blockers fail"): a real LTX clip with 2 major issues and 2 failed checks
   passed at 0.72.
2. *Too strict* (v2–v3 plus "2+ majors fail", then the keyframe gate): in the first full real run
   the gate rejected **10 of 10** keyframes. Eyeballing them showed most rejections were about
   staging (light side, prop placement, a lower fifth that was not perfectly empty), not about
   whether the ad works. A gate that rejects everything is as useless as one that accepts everything.
3. *Hard/soft split* (current; `qa_keyframe` v2, `qa_critic` v5):
   - **HARD** checks can fail a shot on their own: product readable (`product_visible`), product
     and character identity vs. the bible checklist (`continuity_ok`), no text, logos or garbled
     letters (`text_free`, new, default true for stored verdicts), brand safety.
   - **SOFT** categories (`lighting`, `composition`: light side, unrequested background objects,
     prop and doorway placement, exact composition, negative-space precision) are **capped at minor
     by code**, whatever severity the LLM chose. They only lower the score (prompted at about −0.05 each).
   - Pass ⇔ no hard check false AND no blocker AND score ≥ threshold. The "2+ majors" rule is gone.
     Soft issues never force a keyframe regeneration on their own.

**Re-judging the 14 stored keyframes** (no fal spend, $0.17 on Anthropic) moved the verdicts from 1/14
passing to 11/14. Eyeball check: 12 of 14 new verdicts are right. The 2 errors are both **false passes on a
round cap that should be squared** (the known fine-identity gap, addendum 2). The old gate had wrongly
rejected 9 of the 14.

**Consequences.** + The gate now stops broken products and stray text, not staging details.
− Screen-direction mistakes (e.g. a doorway on the wrong side) now pass as soft issues. That is the
right trade for a single bounded attempt, but a continuity-sensitive storyboard may want "screen
direction" promoted to hard. − Fine product identity still depends on perception (product reference
image: MORE_TIME.md).
