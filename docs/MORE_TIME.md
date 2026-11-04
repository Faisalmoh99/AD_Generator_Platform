# With more time

Items found while building and testing on real media. Each one says what it would fix and what
evidence prompted it. M12 folds this list into the README.

1. **Escalation ladder for repeated failures.** When the same failure category repeats after a
   rewrite (e.g. the product is veiled again, or the key light is on the wrong side again),
   do not retry the same request. Escalate instead: (a) a different model for that phase (another
   video model, or another keyframe model), then (b) a spec simplification, applied as a scene
   edit, then (c) give up with a clear reason. Evidence (M11): S4 asked for a passing shadow over the
   hero bottle. LTX rendered it as a veil 2 of 3 times, and Kling ignored it and added smoke. Each
   rewrite only changed the wording of a request the model could not satisfy.
2. **Critic second opinion near the threshold.** Only when |score − threshold| ≤ 0.1 or a single
   major issue decides the verdict; both opinions must pass (ADR-0016 addendum 2).
3. **Product reference image** as an identity anchor, both for keyframe generation (image-conditioned
   keyframes, or Kling `elements`) and for the critic. Text alone missed a round-vs-squared cap 2 of 2
   times, and FLUX produced the wrong cap in 2 of 4 keyframes.
4. **Keyframe model adherence.** FLUX dev repeatedly ignored "key light from camera-left", added
   unrequested plants and props (including one with garbled letters), and filled the "empty lower
   fifth". It has no negative-prompt field. Try `flux-2-flex` or an image model with negative prompts
   or layout control, and/or composite the tagline-safe area in post.
5. **"Dust motes" summon smoke on Kling.** Both Kling clips whose motion prompt mentioned drifting
   dust motes grew a smoke wisp near the cap. Treat particle words as literal-noun risks and test
   that hypothesis before adding it as a rule.
6. **Cross-scene consistency check** (same man, same bottle across scenes): compare each approved
   keyframe with the first approved one of the same subject (deferred in ADR-0016).
7. **Keyframe generation that obeys layout.** In the first full real run FLUX dev produced 0 approved
   keyframes out of 10 (doorway on the wrong side, the hero product in the foreground instead of
   small in the background, props carrying letters). Options: a stronger or layout-controllable
   image model, image-conditioned keyframes (a reference composition or product photo), editing a
   near-miss keyframe (inpainting the one wrong region) instead of re-rolling the whole image, or
   simpler shot specs written with the keyframe model's limits in mind.
8. **Account-level failures should pause the run.** An "exhausted balance" 403 currently fails
   the one scene that hit it. It should pause all paid work and surface a single clear message.
9. ~~**Critic: geometry stability.**~~ **Done** (ADR-0016 add. 4). Flag objects or architecture that appear or disappear between
   the keyframe and the clip frames (seen: a doorway materialising in the majlis S1 wall).
10. ~~**Critic: wardrobe and anatomy plausibility**~~ **Done** (ADR-0016 add. 4) (seen: an unnatural ghutra drape in S1).
11. ~~**HARD check: no alcohol resemblance.**~~ **Done** (`alcohol_free`, ADR-0016 add. 4). The single-take bottle (round body, tall black cap)
    reads as whisky. For Gulf audiences this is brand safety, not style. Make it a hard keyframe-gate
    and clip check, and tell the prompt writer to avoid decanter/whisky silhouettes for perfume.
12. ~~**Crash-safe fal resubmission.**~~ **Done** (ADR-0015 addendum): the request id is persisted at submit and resumed by polling. Persist the fal request id (and idempotency key) in the account
    ledger at submit time; on resume, poll the existing request instead of submitting again. Today
    the in-flight handle lives in memory, so a process killed mid-call pays twice for that clip.
13. **Money-shot composition should be a hard check.** Composition is soft (a viewer forgives a
    prop on the left instead of the right), but for the shot the whole ad hangs on, the
    composition IS the spec. In the hero v2 round the gate passed an S2 mist keyframe at 0.72 while
    listing "vertical glitter column, not a horizontal burst" and "large bokeh discs" as minor.
    Mark one scene as the money shot (storyboard field or user flag) and make its
    spec-defining composition items hard at the keyframe gate and clip QA. Also: FLUX dev ignores
    "burst from the left edge" for mist; try FLUX.2 flex (stronger prompt adherence) for that shot.
