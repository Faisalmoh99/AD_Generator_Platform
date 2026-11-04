# PLAN V2: from hand-fixed shots to a system that makes strong ads

Status: **plan for review, nothing built.** The brain (explicit state machine, typed deltas,
snapshots), recovery, edits, budget/wallet and the UI stay as they are. V2 adds *creative
capabilities* so that a short request produces a strong ad with zero manual intervention.

> Missing input: `docs/HANDOFF.md` (with the two montage references) is not in the repo. The plan
> uses the montage grammar from the brief (8–10 beat-cut shots of 1–1.5 s, product/physics-led).
> Once the references are added, their measured grammar (shot count, cut rhythm, shot types) goes
> into `config/formats.toml` as data; no code changes needed.

## Why: every manual fix in the hero rounds, mapped to the capability that replaces it

| Done by hand (hero v2, Step A) | Capability (item) |
|---|---|
| Picked a 3-shot structure, rewrote shots around what Kling can do | 1 format library, 2 shot design |
| Re-specced S2 three times ("macro mist, no bottle, void, horizontal burst") | 2 shot vocabulary, 7 escalation |
| Re-specced S1 to a hand/jawline detail shot after a full-figure "kurta" | 2 people in detail framing, 4 wardrobe lexicon |
| Re-judged and adopted packshot 2, chose Kontext direct vs composite for scale | 3 automatic identity pipeline |
| Reworded specs to dodge flag triggers ("spray", "perfume"), corrected false-positive flags | 2 (vocabulary declares `needs_product` / `shows_person`) |
| Patched the bible wardrobe (thobe, not kurta; concealed placket) | 4 wardrobe lexicon |
| Human override on S1 (the spray was there, the 3 s clip head wasn't the moment) | 5 peak-moment window |
| Decided which shots deserve two takes | 6 best-of-N on key shots |
| Re-rolled the same failing shot by hand, then simplified it | 7 escalation ladder |
| (not done yet) music and cutting | 8 beat-cut assembly |

## Items, ordered by impact on ad quality

Minimum viable cut-offs:
- **Cut A (needed for the proof run, which stops at keyframes): items 1–3 + 4.**
- **Cut B (needed for a finished, watchable montage): + items 5 and 8.**
- Items 6–7 make it robust; they matter once clips are paid for.

### 1. Ad format library, montage by default (≈2.5 h)
- **Exists:** single-take format (ADR-0017: ideate v4 / select v4 / storyboard v4, one-scene
  validators); `requested_shots` enforced; per-model `render_window` (render 3 s, keep less).
- **New:** `config/formats.toml` with each format's grammar as data:
  `montage` (8–10 shots, 1.0–1.5 s kept each, hero product last, ≤ 2 person-detail shots, no
  dialogue), `single_take` (1 shot, ≤ model max), `narrative` (3–5 scenes, 2–4 s).
  `domain/formats.py`: `AdFormat` + `FormatGrammar`; `Concept.format`, `Storyboard.format`.
  `MAX_SCENES` → per format (10); `MIN_SCENE_S` → 1.0 for montage *kept* time (render stays ≥ 3 s).
  Ideate v5 proposes per format; product ads with no format in the request get ≥ 2 montage
  concepts (code checks it, repair if not); select v6 sees each format's risk notes. Storyboard
  v6 renders the chosen grammar; a code validator enforces it (→ schema repair).
- **Tests:** grammar validator (counts, kept durations, hero last, person-shot cap); scripted
  montage fixture end to end on mock media; "no format requested → montage offered".

### 2. Model-aware shot design: a shot vocabulary (≈1.5 h)
- **Exists:** capability profiles (strengths/weaknesses, observed vs expected) injected into
  ideate/select/storyboard; spray-is-mist rule; particle ban; key-action checks.
- **New:** `config/shot_vocabulary.toml`: ~14 entries, each with `framing`, `needs_product`,
  `shows_person`, `identity_mode` (none | direct | composite), typical motion, `fit` per video
  endpoint, `key_shot`, and a `fallback` entry for item 7. Physics-led: macro facet sweep, light
  sweep across glass, amber drop into water, splash, mist burst past a backlight (no bottle),
  sand/silk past the cap, turntable glint, cap lift macro, hero push-in. People in detail framing
  only: hand + cuff, jawline to chest, back silhouette. No full-figure person or regional garment
  in full view unless the request asks for it (code check on `shot_type` + `shows_person`).
  Storyboard picks `shot.vocab` per shot; flags come from the vocabulary, not from text
  matching, which removes the "spray"/"perfume" flag-trigger workarounds.
- **Tests:** vocabulary loads and validates; storyboard with a full-figure person and no request
  is repaired; flags derived from the vocabulary.

### 3. Automatic product identity pipeline (≈1 h)
- **Exists:** reference packshot + strict gate (`qa_product_ref` v3, salient vs fine), Kontext
  direct mode (ADR-0003 add. 3), composite mode with Kontext multi (add. 4), `valid_for`,
  fallback when no packshot passes.
- **New:** on by default for physical-product ads; the mode is chosen **per shot by code** from the
  vocabulary: product alone/hero → `direct` (packshot scale is right), product in a hand or small in
  a scene → `composite` (scene scale), no product → plain. Replaces the global
  `product_reference_mode` env.
- **Tests:** routing per shot (mock providers count calls per mode); a reference regenerated only
  when the product section changes (exists).

### 4. Cultural wardrobe lexicon in config (≈45 min)
- **Exists:** bible character/wardrobe, wordless/effect lints in the continuity node.
- **New:** `config/culture.toml`: Saudi thobe (stiff stand collar or classic collar, concealed
  placket with no visible buttons, ankle length, optional cufflinks), ghutra/shemagh (white ghutra
  or red-white shemagh on the head, black agal, drape rules), and look-alikes to avoid (kurta:
  mandarin collar + visible button placket; Emirati kandura tassel; Omani dishdasha).
  Injected into continuity and keyframe prompts. A deterministic lint on the bible's wardrobe
  (e.g. "button placket", "kurta") → repair. The keyframe gate gets the same lexicon for its
  anatomy/wardrobe check.
- **Tests:** lint catches the kurta phrasing from the Step A bible; lexicon renders into prompts.

### 5. Peak-moment selection (≈1.5 h)
- **Exists:** `render_window` (rendered vs kept seconds); assembly trims from the clip head;
  dense sampling for key actions; frame timestamps in the critic's images.
- **New:** pick each shot's best kept window (1–1.5 s) inside the rendered clip:
  `media/frames.py: motion_profile()` (ffmpeg frame differences, deterministic) chooses the
  peak-motion window, constrained so a key action (vocabulary) falls inside it; the critic is then
  shown dense frames **of that window** and judges it. `Scene.use_window = (start_s, dur_s)`;
  assembly uses `-ss start -t dur`. This is the S1 lesson: the spray was at ~2 s, the head wasn't
  the moment.
- **Tests:** synthetic clip with a burst at 2.0 s → window covers it; assembly honours the window;
  critic sampling stays inside it.

### 6. Music + beat-cut assembly (≈1.5 h)
- **Exists:** `add_music` (user music only: loop, trim, fade), concat, end card, duration check.
- **New:** `assets/music.mp3` is user-supplied and licensed; nothing is bundled.
  `media/beats.py`: onset/tempo from PCM decoded by ffmpeg (numpy spectral flux, no heavy deps),
  then shot boundaries snap to the nearest beat within each shot's window slack (±0.2 s), the hero
  shot lands on a downbeat, and the end card starts on a beat. Without music, the planned durations
  are used.
- **Tests:** synthetic click track at 120 bpm → cuts within 20 ms of beats; total duration kept.

### 7. Escalation ladder (≈1.5 h)
- **Exists:** classified recovery (policy rewrite, QA regeneration with critic fixes, keyframe
  gate retries), failure signatures in attempts, MORE_TIME item 6.
- **New:** `agent/recovery/escalation.py`: the same failure signature (check + category) twice on
  a shot → step up automatically: (1) rewrite with fixes (today), (2) simplify the spec (drop the
  secondary action; tighter framing), (3) swap to the vocabulary entry's `fallback` (e.g. "spray at
  the neck" → "mist burst, no person"; "full figure" → "hand + cuff"). Each step is a typed event;
  the brain is unchanged (it is a recovery strategy inside the scene runner).
- **Tests:** two identical rejections → simplify; a third → fallback entry; budget respected.

### 8. Best-of-N on key shots (≈30 min)
- **Exists:** best-of-N clips (`clip_candidates`, reserved up front, code picks the best with no
  hard failure; ADR-0003 add. 2).
- **New:** N = 2 only for vocabulary `key_shot` entries (hero push-in, the money shot), and only
  if the budget covers both takes after all other shots are reserved; otherwise 1.
- **Tests:** key shot gets 2 candidates, others 1; tight budget falls back to 1.

**Total:** Cut A ≈ 5.75 h; Cut B + ≈ 3 h; items 7–8 + ≈ 2 h. Every item lands with tests and
`make check` green, conventional commits, and ADRs: 0018 formats and vocabulary, 0019 identity
routing and the wardrobe lexicon, 0020 peak window and beat cutting.

## Proof run (after Cut A): fully automatic, no human fixes

```bash
ADSMITH_LLM_PROVIDER=anthropic ADSMITH_MEDIA_PROVIDER=fal ADSMITH_FAL_VIDEO_MODEL=kling_v3_standard \
uv run adsmith run "سوّ لي إعلان 12 ثانية لعطر رجالي فاخر للشباب، فخم وجريء" --auto --until generate
uv run python scripts/contact_sheet.py <run>      # new: reference + every keyframe + gate verdicts
```

No concept picking, no spec edits, no flag fixes, no overrides. It stops before any video.
The output is the contact sheet with the reference packshot, every keyframe and its gate verdict
(and any automatic escalations), judged by eye against the montage references.

**Estimated fal cost (stills only).** 12 s − 2 s end card ≈ 10 s, so about 8 shots at 1.25 s:
1 packshot ($0.025) + 3 direct ($0.04) + 2 composite ($0.065) + 3 plain ($0.025):
- **first pass ≈ $0.35**
- **with typical gate retries (~1.4 tries per shot) ≈ $0.50**
- worst case with `keyframe_max_tries=2` ≈ $0.73

Proposed hard cap: **$0.60** via the wallet ceiling. If the cap is hit, the guard refuses the
remaining stills and the sheet shows the gaps (no partial overspend).

**Budget reality:** the fal balance is about $0.73. The montage's clips (8 × 3 s × $0.084 ≈ $2.02,
plus best-of-N on 2 key shots ≈ $0.50) can't be afforded after the proof run. A finished V2 ad
needs a top-up of about $3; otherwise the demo shows the automatic keyframe sheet plus the existing
real clips.

## Parked
Step A (S1 close-up on fork `run_01M6MCGVC2YFM2RHRCEWJFZZMP`) is waiting at the approval gate. It
is superseded by this plan: no clip will be generated for it.
