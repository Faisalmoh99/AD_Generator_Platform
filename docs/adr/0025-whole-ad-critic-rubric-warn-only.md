# ADR-0025: The whole-ad critic: the hard gate decides, the creative rubric warns

- Status: Accepted (2026-11-06)

## Context
A multishot run (ADR-0023) produces one finished ad, not eight clips. The per-shot critic
(`qa_critic` v8) judges a clip against its keyframe; it cannot say whether the whole film has an
idea. The user decided: the critic scores the full video on the same creative rubric as the
concepts (ADR-0024) plus the existing hard execution gate; a creative fail is warn only; and for
Sunday's ≈ $5 budget a hard fail is warn only too: no paid regeneration is triggered by code.

## Decision
1. **`qa_ad_critic` v1** receives the real product photo (image 0), three frames per planned shot
   (20/50/80 % of each shot's window on the real video's timeline, labelled with absolute time),
   the concept, the storyboard, the prompt the model received, the product checklist and the
   rubric's video questions. It fills the existing `QAChecks` and issues, a note per shot
   (delivered or not) and the six rubric scores, in two separate jobs.
2. **Code decides.** `passed` = the existing hard rules (`QAVerdict.fail_reasons`): product
   readable, product identity vs the photo, no text, brand-safe, alcohol-free, blockers, score
   below threshold. A "product" issue is never minor and fails `continuity_ok` (as at the keyframe
   gate). The creative score is `Rubric.weighted` over the six scores; below
   `creative_threshold` (6.5) it is a warning; undelivered shots are warnings.
3. **Warn only.** Pass → approved; hard fail → flagged; both are assembled; the creative review
   is a decision event (level warn when there are warnings). The human decides with
   `adsmith accept-ad` or `adsmith regenerate-ad` (back to the estimate gate).
4. **Consistency rules** feed schema repair: every failed check explained by an issue, every
   frame index an image that was sent, exactly one note per planned shot.
5. **Calibration before tuning.** `scripts/calibrate_ad_critic.py` runs the same critic on the
   old pipeline's montage v2 (shots from the logged beat-cut durations, the run's packshot as the
   reference). Expected: hard gate passes, creative score low (insight, story_turn and hook
   weakest). A high score means the rubric is broken and is reported before any tuning.

## Alternatives considered
- Let the LLM decide pass/fail: rejected since ADR-0016; code decides from typed fields.
- Fail the run on a low creative score: a judgement, not a measurement, and the paid video
  already exists; the user chose warn only.
- Re-use `qa_critic` with more frames: its rules are per-clip (keyframe continuity, kept window).

## Consequences
+ One review tells the client both "is it usable" and "is it an idea", on the scale the concept
  was chosen with, so the trace can show concept score vs film score.
− Audio is not judged (the critic sees frames only).
− The planned timecodes are approximate in a real generation; a shot note may cover a
  neighbouring shot's frames.

## Addendum (2026-11-07): critic v3 (ADR-0026 phase 5)

- **Windows from the video's own cuts.** `media/frames.py` `detect_cuts` finds hard cuts by code
  on the existing `motion_profile` (24 fps, 96x96 grey): a frame-difference spike above 20 and 3x
  the median of the surrounding second. Calibrated on try 2 (cuts 1.25 / 3.63 / 5.42 s against
  ffmpeg scene detection's 1.23 / 3.60 / 5.40) and try 1 (its crossfade is not a cut). When the
  count matches the plan (shots - 1), `ad_shot_windows` uses the observed shots; otherwise the
  scaled plan as before. A decision event logs observed vs planned cuts. Limitation: the measure
  is luma, so a cut between two shots of the same brightness and different hue is missed (then
  the scaled plan is used).
- **Dense where it matters.** The key shot (`AdPrompt.key_shot`; for a human prompt the longest
  shot) gets 8 frames instead of 3 (try 2's 0.5 s morph fell between samples), and three extra
  frames at 0.5 / 1.0 / 1.5 s judge the hook. About 23 images per review instead of 13-15.
- **Execution checks** (`qa_ad_critic` v3, `AdExecutionChecks`): product handled, seconds on
  screen, the first two seconds hook, the key moment visible, one location, and a note. They are
  warnings only (the hard gate is unchanged); a false check or screen time under half the film
  needs a reason in the note (code, → repair). Stored verdicts made before v3 load (default None).
- **Deviation from the plan**: "every false flag needs an issue" became "needs a reason in the
  note": an issue in the "product" category would have failed the hard gate by code
  (`ad_product_rule`), turning a warning into a hard fail.
