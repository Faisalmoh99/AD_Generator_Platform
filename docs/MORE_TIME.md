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
