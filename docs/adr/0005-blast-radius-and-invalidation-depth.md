# ADR-0005: Blast radius and invalidation depth for scene edits

- Status: Accepted (2026-11-04)

## Context
Users edit in natural language ("make scene 3 at night", "خل المشهد الثاني أبطأ", i.e. "make the
second scene slower"). Redoing the whole job is slow and expensive. Redoing only the target can
break continuity with its neighbours or with the continuity bible.

## Decision
1. **Parse** the edit into a typed `ScenePatch` (LLM). Scene references (ordinals in EN/AR) are then
   resolved **deterministically**. Low confidence → ask a clarification question instead of guessing.
2. **Conflict check:** deterministic rules (duration sum, time-of-day jumps, bible fields) plus an
   LLM semantic check against neighbours and the bible → `ConflictReport` with a suggested
   resolution (`apply_local | propagate_neighbors | update_bible | reject`).
3. **Blast radius** is a pure function `(storyboard, bible, patch, conflicts, resolution) →
   {scene_id: StaleFrom}` with an **invalidation depth** per affected scene:
   - `clip` (keep keyframe, redo video): `camera_movement`, `speed`, `duration`
   - `prompt` (re-prompt, new keyframe, new clip): lighting, time of day, shot type, lens,
     composition, setting, action, mood, description
   - bible change → `prompt` for **only the scenes that use the changed bible section**
   - duration change → proportional rebalance of other scenes at `clip` depth to keep the total
4. Affected scenes get a new `SceneVersion` (history kept) and `status=stale`. The orchestrator
   re-enters at the shallowest needed stage. Unaffected scenes keep their `content_hash`, so the
   cache ensures they are never regenerated.

## Alternatives considered
- Always regenerate the target only: fast, but silently breaks continuity.
- Always regenerate everything: correct but defeats the requirement.
- Let the LLM decide which scenes to redo: not testable and not explainable. The LLM's role is
  limited to *detecting* semantic conflicts; the set computation is code.

## Consequences
- + Minimal, explainable re-work. Each affected scene carries a human-readable reason in the trace.
- + The algorithm is pure and covered by a parametrised matrix plus a property test ("unaffected
  scenes' hashes never change").
- − The field→depth table is a judgement call. It lives in one place and is documented.

## Addendum (M7): what the implementation added
- **`motion_change`** on the patch: motion-only adjustments the enums cannot express ("slower" for
  a scene already in `slow_motion`). Clip depth; a `spec_edit` rewrite updates only the motion
  prompt, and the kept keyframe is enforced byte-identical.
- **References are resolved by code** (`agent/edit/refs.py`, AR/EN ordinals, Arabic-Indic digits,
  "last"/"الأخير"). Low confidence, a clarification question or an unresolved reference ⇒ ask,
  never guess. A bug caught by a test: "one" in the ordinal table made "the nice one" mean scene 1.
- **Conflicts**: deterministic time-of-day findings are fed to the `conflict_check` LLM, which
  keeps the real ones (a neighbour continuing the same moment) and drops separate moments.
  Conflicts may name a scene by number; code maps it to the id with the same resolver.
- **Apply is one atomic commit**; clip-depth scenes are requeued at once, prompt-depth scenes
  re-enter at `prompt`. Untouched scenes are never re-entered, so they make zero provider calls (not
  merely cache hits). A duration change rebalances the others at clip depth; when their rendered
  length does not change, the clip is a cache hit.
