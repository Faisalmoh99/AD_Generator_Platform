# PLAN V2: from hand-fixed shots to a system that makes strong ads

Status: **approved with changes (2026-11-05); build in a fresh session.** The brain (explicit state machine, typed deltas,
snapshots), recovery, edits, budget/wallet and the UI stay as they are. V2 adds *creative
capabilities* so that a short request produces a strong ad with zero manual intervention.

## The two montage references (described, not copied)

Their measurements go into `config/formats.toml` (format `montage`) as **data**; the code only
reads the grammar.

| | (a) Dark, moody fragrance ad | (b) Product video "made from one product photo" |
|---|---|---|
| Length / shots | ~16 s, ~14 shots (≈1.1 s per shot) | ~22 s, ~12 shots (≈1.8 s per shot) |
| Shot types | extreme macros of the bottle and cap (brushed-metal texture, an engraved logo, glass edges); light sweeping across surfaces; rim light in darkness | a water drop hitting a leaf; droplets on glass; the bottle falling into water with splashes and rising bubbles; a ribbon of water swirling around the bottle; one spray burst; the bottle on dark rock; macro of the cap |
| People | a man's silhouette only at the very end | none |
| Look | dark, low-key, rim-lit, metallic textures | dark backgrounds, water physics, crisp product |
| Identity | product details carry the ad | **same bottle everywhere because everything comes from one reference image** (exactly item 3) |

Derived montage grammar (initial values, tunable in config): 10–14 shots; kept duration
1.0–1.8 s per shot (render 3 s, keep the peak window, item 5); ≥ 70% product/physics shots;
≤ 1 person shot, silhouette or detail framing, placed last or second to last; hero product last;
light sweeps, rim light and macro textures preferred; liquids (drop, splash, swirl, bubbles) and
one spray burst allowed as the physical events. Note for (a): an *engraved logo* is not possible
for us (wordless products, no invented text), so "engraved" becomes texture only (brushed metal,
facets).

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

## Build order and stops (approved)

1 → 2 → 3 → 4 → **STOP 1** → 5 → 6 → **STOP 2**. Item 8 is small wiring (best-of-N already exists)
and is done alongside item 6. Item 7 (escalation ladder) moves to `docs/MORE_TIME.md`.

- **STOP 1, after item 4:** the proof run (below): `--auto`, no human fixes, keyframes only,
  hard cap **$0.60**. Report the contact sheet **and what the system decided at each stage**
  (format, concept, shot vocabulary per shot, identity mode per shot, gate verdicts, escalations
  or fallbacks), all read from the trace.
- **STOP 2, after item 6:** a mock end-to-end montage with beat cutting, plus test results.
- **No clip money** until the user tops up after seeing the STOP 1 keyframes.
- **Time estimates have run 2–3× too high on past milestones** (the work went faster than
  planned). Treat the hours below as upper bounds, not a schedule.

## Items, in build order

### 1. Ad format library, montage by default (≈2.5 h)
- **Exists:** single-take format (ADR-0017: ideate v4 / select v4 / storyboard v4, one-scene
  validators); `requested_shots` enforced; per-model `render_window` (render 3 s, keep less).
- **New:** `config/formats.toml` with each format's grammar as data:
  `montage` (8–10 shots, 1.0–1.5 s kept each, hero product last, ≤ 2 person-detail shots, no
  dialogue), `single_take` (1 shot, ≤ model max), `narrative` (3–5 scenes, 2–4 s).
  `domain/formats.py`: `AdFormat` + `FormatGrammar`; `Concept.format`, `Storyboard.format`.
  `MAX_SCENES` → per format (10); `MIN_SCENE_S` → 1.0 for montage *kept* time (render stays ≥ 3 s).
  **Changed at review (§Implementation detail):** one format per run, resolved by code from the
  brief (`Brief.format`, copied to `Storyboard.format`); ideate v5 writes all three concepts in
  that format; `select` stays v5. Storyboard v6 renders the chosen grammar; a code validator
  enforces it (→ schema repair).
- **Tests:** grammar validator (counts, kept durations, hero last, person-shot cap); scripted
  montage fixture end to end on mock media; "no format requested → montage offered".

### 2. Model-aware shot design: a shot vocabulary (≈1.5 h)
- **Exists:** capability profiles (strengths/weaknesses, observed vs expected) injected into
  ideate/select/storyboard; spray-is-mist rule; particle ban; key-action checks.
- **New:** `config/shot_vocabulary.toml`: ~14 entries, each with `framing`, `needs_product`,
  `shows_person`, `identity_mode` (none | direct | composite), typical motion, `fit` per video
  endpoint, `key_shot`, and a `fallback` entry for item 7. Physics-led, from references (a) and (b): extreme macro of cap and glass edges (brushed metal,
  facets), light sweep across surfaces, rim light in darkness, water drop hitting a leaf, droplets
  on glass, bottle falling into water (splash, rising bubbles), a water ribbon swirling around the
  bottle, one spray burst, bottle on dark rock, cap macro, hero push-in. People in detail framing
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

### 7. Escalation ladder → moved to `docs/MORE_TIME.md` (item 15)

### 8. Best-of-N on key shots: wiring only (≈30 min, done with item 6)
- **Exists:** best-of-N clips (`clip_candidates`, reserved up front, code picks the best with no
  hard failure; ADR-0003 add. 2).
- **New:** N = 2 only for vocabulary `key_shot` entries (hero push-in, the money shot), and only
  if the budget covers both takes after all other shots are reserved; otherwise 1.
- **Tests:** key shot gets 2 candidates, others 1; tight budget falls back to 1.

**Total (upper bound):** items 1–4 ≈ 5.75 h; items 5–6 + 8 ≈ 3.5 h. Every item lands with tests and
`make check` green, conventional commits, and ADRs: 0018 formats and vocabulary, 0019 identity
routing and the wardrobe lexicon, 0020 peak window and beat cutting.

## Proof run (STOP 1, after item 4): fully automatic, no human fixes

```bash
ADSMITH_LLM_PROVIDER=anthropic ADSMITH_MEDIA_PROVIDER=fal ADSMITH_FAL_VIDEO_MODEL=kling_v3_standard \
uv run adsmith run "سوّ لي إعلان 12 ثانية لعطر رجالي فاخر للشباب، فخم وجريء" --auto --until generate
uv run python scripts/contact_sheet.py <run>      # new: reference + every keyframe + gate verdicts
```

No concept picking, no spec edits, no flag fixes, no overrides. It stops before any video.
The output is the contact sheet with the reference packshot, every keyframe and its gate verdict,
plus a decisions report from the trace (format, concept and why, vocabulary and identity mode per
shot, gate verdicts), judged by eye against references (a) and (b).

**Estimated fal cost (stills only).** Footage = the requested 12 s (the end card is added on top, unchanged), so about 9 shots at ~1.33 s:
1 packshot ($0.025) + ~5 direct ($0.04) + ~1 composite ($0.065) + ~3 plain ($0.025):
- **first pass ≈ $0.37**
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

## Implementation detail (2026-11-05, pre-build review)

### 0. What this session understood

1. **Brain:** a declared transition table (`agent/transitions.py`), `next_stage` pure; nodes
   return typed deltas (`agent/deltas.py`); one writer per run commits deltas + events + a
   snapshot (`storage/repo.py`); scene workers (`agent/scenes.py`) commit per scene with
   scene-version checks; recovery is bounded and typed; edits compute a pure blast radius.
2. **Media path:** prompt → keyframe (FLUX dev, or Kontext from the locked packshot in direct or
   composite mode) → keyframe gate (`qa_keyframe` v5 + `gate_draft`: any product mismatch hard,
   salient vs fine) → clip (Kling, render ≥ 3 s, assembly trims the head) → critic (dense frames
   for brief events) → assemble + end card.
3. **Money:** run budget reserve/settle + cross-run wallet ledger with a hard ceiling
   (`ADSMITH_FAL_BALANCE_USD = settled + cap`); fal balance ≈ $0.73; no clip money before top-up.
4. **Real-run lessons that drive V2:** video models do physics, not acting; product identity is
   the #1 failure and starts in the keyframe (fixed by the locked reference); nouns are literal
   (no "curtain shadow", no particles); blank surfaces grow text; the critic misreads brief
   events and the clip head is not the moment; regional wardrobe drifts to a kurta; FLUX dev
   ignores some layout requests; the judge plays it safe.
5. **Manual fixes PLAN_V2 turns into rules:** structure and shot rewriting (1, 2); re-speccing
   failing shots and flag workarounds (2; ladder parked in MORE_TIME 15); packshot adoption and
   direct-vs-composite choice (3); thobe-not-kurta bible patch (4); the S1 "spray at 2 s" override
   (5); two takes on the money shot (8); music and cutting (6).
6. **Stays untouched:** transitions, deltas, snapshots, recovery, edits, budget/wallet, UI.

### 1. Critical review of PLAN_V2

**Missing, and the references rely on it (all added as data, little code):**
- **Lighting design per shot.** The references are one look: dark, low-key, a single hard rim
  light on glass and metal. Today the bible has a `lighting` baseline and each shot a free-text
  `lighting`, both LLM-chosen. Fix: the format carries a `look` paragraph (`formats.toml`) that is
  injected into continuity (becomes the bible's `lighting` + `color_grade` brief) and each
  vocabulary entry carries a `lighting` default. No new validator.
- **Camera-move vocabulary.** `CameraMove` exists; what is missing is the rule that montage shots
  use slow, simple moves only. Fix: `allowed_camera` in the montage grammar (static, push_in,
  dolly_in, orbit, tilt, rack_focus) enforced by the grammar validator, plus a `camera` default
  per vocabulary entry. `speed_default = slow_motion` for physics shots.
- **Colour consistency across shots.** Already handled: the bible's `color_grade` is in every
  prompt, Kontext keeps the product, and the format `look` pins the background family (black /
  charcoal / wet dark rock). The only new rule: montage uses ≤ 3 distinct `setting` families,
  stated in the storyboard prompt, not validated. MORE_TIME 6 (cross-scene image check) stays
  parked.
- **Sound design.** Out of scope beyond music (item 6): Kling `generate_audio` stays off; SFX would
  be a new provider. One line in MORE_TIME.
- **Transitions.** The references are hard cuts on the beat; item 6 does exactly that. The end
  card already cross-fades. Nothing else.
- **Not in the plan but cheap and important:** a `decisions` report script (the STOP 1 deliverable
  is "what the system decided"); `max_total_attempts` (40) must grow to 80 for 10-shot boards.

**Over-engineered for today, cut or shrunk:**
- `narrative` as a *new* format with its own validators: it is today's behaviour. It becomes the
  name of the existing rules (1 single take or 2–6 scenes, 1.5–8 s); zero new validation.
- `fit` per video endpoint in every vocabulary entry (14 × 3 cells to maintain): replaced by one
  optional `avoid_on = [...]` list; empty for all entries today.
- `Concept.format` + "≥ 2 montage concepts" + select v6 format scoring: replaced by the decision
  above (one format per run, resolved by code). `select` keeps v5.
- `Storyboard.format` is kept (self-contained validation on load, like `target_duration_s`).
- The escalation ladder stays in MORE_TIME 15; the vocabulary's `fallback` field is data only.

**Risks, corrected:**
- *FLUX dev layout adherence:* real, but in a montage FLUX dev only renders the packshot and the
  product-free shots (water drop, mist burst). Product shots come from Kontext. Each product-free
  entry carries a `keyframe_hint` written in phrasing that worked (void, horizontal burst). No
  model switch now; `flux_2_flex` for `plain` shots is a one-line catalog change later.
- *Kontext scale:* direct mode keeps the packshot scale, which is right for product, macro and
  hero entries. **New, unverified risk:** whether Kontext can *re-frame* the 2/3-frame packshot
  into a true extreme macro (cap filling the frame). `keyframe_reference` v2 states the re-frame
  explicitly. STOP 1 answers it for $0.04 per macro shot; if it fails, macro entries fall back to
  `composite` (plain macro keyframe, then swap).
- *Kling minimum 3 s:* no exposure at STOP 1 (stills only). For clips: render 3 s, keep 1.0–1.5 s;
  Kling's first ~0.5 s often ramps up slowly, which is exactly why the kept window must be chosen
  by motion (item 5), not the head.
- *Critic variance:* still the main quality risk, but montage shots are the easy case (no people,
  no acting; identity is salient-vs-fine against a checklist). For the proof run
  `ADSMITH_KEYFRAME_MAX_TRIES=2` bounds the money; the gate's verdicts are part of the report
  and judged by eye.
- *Not listed before:* the judge could have picked a non-montage concept (fixed by the format
  decision); the proof run's budget guard refuses stills when the cap is hit, which the sheet
  must show as gaps, not as an error.

### 2. Implementation breakdown

Conventions: new config is TOML loaded through `Settings` cached properties (like `catalog`);
new domain code lives in `domain/`; every validator feeds the schema-repair loop through the
node's `convert`; prompts bump versions; nothing in the transition table changes.

#### Item 1. Format library (`montage` by default)

**Config `config/formats.toml`** (data; the code reads the grammar):

```toml
default_physical = "montage"     # physical product, no format in the request
default_other = "narrative"      # apps, services, places

[formats.montage]
label = "Physics-led product montage"
min_shots = 8
max_shots = 12
kept_s = [1.0, 1.5]                # per shot in the ad; the model renders its minimum (Kling 3 s)
hero_last = true
max_person_shots = 1
person_framing = ["detail", "silhouette"]
person_position = "last_two"       # the one person shot is last or second to last
min_product_share = 0.7            # shots whose entry needs_product or is a physics event
allowed_camera = ["static", "push_in", "dolly_in", "orbit", "tilt", "rack_focus"]
speed_default = "slow_motion"
max_settings = 3
dialogue = false
identity = "reference"             # locked packshot + per-shot Kontext routing (item 3), always on
look = """dark, low-key product photography: black or deep charcoal backgrounds, one hard rim
light tracing glass and metal edges, a soft low fill, crisp specular highlights, brushed metal
and faceted glass textures, no readable text anywhere"""
events = """one physical event per shot and nothing else: a light sweep, a water drop, droplets
forming, a splash with rising bubbles, a ribbon of water, one spray burst, the cap lifting,
a slow turntable glint; never a person acting"""
risk_notes = [
  "every shot is a separate generation; identity comes only from the locked product reference",
  "8-12 keyframes per ad: more gate calls and stills money than a single take",
]

[formats.single_take]
label = "Single-take hero"
min_shots = 1
max_shots = 1
kept_s = [5.0, 15.0]
hero_last = true
identity = "reference"

[formats.narrative]                # today's multi-scene rules, unchanged
label = "Narrative scenes"
min_shots = 1                      # 1 = single take, else 2-6 (existing node logic)
max_shots = 6
kept_s = [1.5, 8.0]
hero_last = true
identity = "optional"              # --product-ref as today
```

**Domain `domain/formats.py`:** `AdFormat(StrEnum)` = montage | single_take | narrative;
`FormatGrammar` (frozen pydantic, fields above, `kept_min/kept_max` properties);
`FormatLibrary.load(path)` with `grammar(fmt)`; `resolve_format(brief) -> AdFormat`
(`brief.requested_format or (default_physical if brief.physical_product else default_other)`);
`grammar_problems(draft, grammar, vocab, brief) -> list[str]` (counts, kept range, hero last,
person count/position/framing, product share, camera allowed, `vocab` required and known).
`Settings.formats` cached property.

**Brief (`domain/brief.py`, `understand` v4):** new fields `physical_product: bool = True`
and `requested_format: AdFormat | None = None` ("only if the client names one: one continuous
shot → single_take; a story or scenes with a character → narrative; fast cuts / montage →
montage; else null"), plus resolved `format: AdFormat = "narrative"` set by code in
`nodes/understand.py` (default keeps stored briefs loading). `requested_shots` `le` → 12.
`concept.estimated_scene_count` `le` → 12. `brief_block()` prints `Format: montage (…)`.

**Storyboard (`domain/storyboard.py`, `nodes/storyboard.py`, `storyboard` v6):**
`MAX_SCENES` → 12, `MIN_SCENE_S` → 1.0 (`domain/scene.py`, `domain/edit.py` bound);
`Storyboard.format: AdFormat = "narrative"` copied from the brief. `scene_duration_bounds()`
gains `fmt`: montage → (kept_min, kept_max, kept_max); narrative/single take → as today
(model minimum applies: render ≥ 3 s is already handled by `render_window` snapping). The
node's `convert` calls `grammar_problems` for montage, keeps today's checks otherwise;
`max_scenes` comes from the grammar. `storyboard` v6: system text unchanged in spirit, plus a
`<format>` block in USER (label, shots N–M, kept range, look, events, the vocabulary list from
item 2, "≤ 3 setting families", "hero product last, person detail shot last or second to last").
`agent/edit/service.py` passes the storyboard's format to `scene_duration_bounds` so rebalance
uses kept bounds for montage.

**Ideate v5:** `<format>` block (label, look, events, risk notes) replaces the "(a)/(b) two
formats" paragraph; three concepts on different creative axes *inside* the format; for montage
`narrative_arc` = 3–5 movements of physical events, no people acting. `select` stays v5
(`format` appears in the brief block; nothing else needed).

**Budget:** `Settings.max_total_attempts` 40 → 80 (10 shots × 3 keyframe tries + clips).

**Fixtures:** `perfume_en`/`perfume_ar` `understand.json` get `"requested_format": "narrative"`
so every existing test and the demo keep today's behaviour. New scenario
`providers/llm/fixtures/montage_en/` (understand with `physical_product: true`, 3 montage
concepts, select, a 9-shot storyboard with `vocab` per shot, continuity, prompt_writer
`{"transform": "scene_prompt"}`, endcard_copy, prompt_rewrite `echo_prompt`).

**Tests:** `tests/unit/test_formats.py` (library loads; `resolve_format` for the three cases;
`grammar_problems` on a good board and on each violation: 7 shots, a 2.5 s shot, hero not last,
two person shots, person shot third, handheld camera, missing vocab);
`tests/integration/test_montage_pipeline.py` (scripted `montage_en` → DONE on mock media, 9
approved scenes, final duration 12 + 2 s; edit of one montage shot regenerates only it).

#### Item 2. Shot vocabulary

**Config `config/shot_vocabulary.toml`** (15 entries; two shown in full, the rest by name):

```toml
[shots.cap_macro]
label = "Extreme macro of the cap"
framing = "macro"                  # macro | product | event | detail | silhouette
needs_product = true
shows_person = false
identity_mode = "direct"           # none | direct | composite (item 3 reads this)
key_shot = false
shot_type = "extreme_close_up"
camera = "push_in"
speed = "slow_motion"
lighting = "one hard rim light raking across brushed metal, everything else in darkness"
motion = "a very slow push-in while a thin band of light slides over the metal facets"
keyframe_hint = "the cap fills the frame edge to edge; brushed-metal grain and facet edges sharp"
fallback = "light_sweep"
avoid_on = []

[shots.spray_burst]
label = "One spray burst, no bottle"
framing = "event"
needs_product = false
shows_person = false
identity_mode = "none"
key_shot = false
shot_type = "close_up"
camera = "static"
speed = "slow_motion"
lighting = "a single hard backlight from behind the burst, black void around it"
motion = "one short horizontal burst of fine mist enters from the left edge, catches the backlight and dissolves"
keyframe_hint = "a horizontal burst of fine mist frozen mid-air against a black void, lit from behind; no bottle, no hand, no surface"
fallback = "light_sweep"
avoid_on = []
```

Remaining entries: `glass_edge_macro` (direct), `light_sweep` (direct, product), `rim_light_reveal`
(direct), `turntable_glint` (direct), `water_drop_leaf` (none, event), `droplets_on_glass`
(direct), `bottle_into_water` (direct, event, splash and bubbles), `water_ribbon` (direct),
`bottle_on_rock` (direct), `cap_lift` (direct), `hero_push_in` (direct, **key_shot**, product
hero, lower fifth clear), `hand_cuff` (composite, detail, shows_person), `jawline_chest`
(composite, detail, shows_person), `back_silhouette` (none, silhouette, shows_person).

**Domain `domain/vocabulary.py`:** `ShotVocab` model (fields above, `identity_mode` must be
`none` iff `needs_product` is false; `fallback` must exist), `VocabLibrary.load`,
`Settings.vocabulary`; `prompt_block()` renders the list for the storyboard prompt and one entry
for the prompt writer. **`SceneDraft.vocab: str | None = None`** (required for montage by the
grammar validator; `SPEC_FIELDS` += `vocab`, so a vocab change is a new keyframe; not in
`MOTION_*`). Flags for vocab scenes are **set by code from the entry** (`shows_character =
shows_person`, `shows_product = needs_product`) in the storyboard node, and `enforce_scene_flags`
skips scenes with a `vocab` (ADR-0018 records this exception to ADR-0010: the vocabulary is the
source of truth, which is what removes the "spray"/"perfume" false positives). `scene_block()`
prints `Vocab: cap_macro (identity direct)` so the prompt writer, rewrite and critic see it.

**Person rule:** in `grammar_problems`, a scene with `shows_character` whose entry framing is
not in `person_framing`, or with no entry, is repaired ("use hand_cuff, jawline_chest or
back_silhouette"); unless `brief.requested_format == narrative`.

**Prompt writer v7:** `<shot_vocab>` block (label, motion, keyframe_hint, lighting, speed,
identity mode); for montage: "the ad keeps the best {{ visible_s }} s of the {{ render_s }} s
render (chosen automatically): make the one event the only thing that happens and let it play
across the whole render; the product is clean and unobstructed at the end"; product-free entries:
"no bottle, no hand" explicitly. `prompt_rewrite` v7 carries the same block.

**Tests:** `tests/unit/test_vocabulary.py` (loads; fallbacks resolve; identity/needs_product
consistency; every entry's `camera` is in the montage `allowed_camera`); storyboard with a
full-figure person and no request → repair text; a `spray_burst` scene whose description says
"perfume" keeps `shows_product=false`; `scene_block` shows the vocab.

#### Item 3. Automatic identity routing

- `MediaGateway.prepare(..., identity_mode)` replaces the global `composite is not None` test:
  `none` → plain; `direct` → `_condition_on` (Kontext); `composite` → `_composite`. The gateway
  always receives both providers when a reference is possible (`context.py`: `comp_ep` no longer
  depends on `product_reference_mode`).
- `SceneStageRunner._prepare` computes the mode: `scene.vocab` → entry.identity_mode; no vocab →
  legacy (`shows_product` → `settings.product_reference_mode`, kept as the non-vocab fallback,
  documented as deprecated). The mode and the vocab go into the `ATTEMPT_STARTED` payload
  ("Scene 3 keyframe attempt 1, identity direct (cap_macro)") for the decisions report.
- **On by default for montage:** `_product_reference` runs when `config.product_reference` is
  true **or** `storyboard.format`'s grammar says `identity = "reference"`. No change to
  `RunConfig`, no churn in narrative tests.
- `keyframe_reference` v2: adds `{{ framing_note }}` from the entry (`macro` → "re-frame to an
  extreme close-up: the cap fills the frame; keep its material and colour exactly"; `product` →
  "keep its scale as a hero object"). `keyframe_composite` v2: generic placement from the entry's
  `keyframe_hint` instead of the hand-specific text.
- Cache/resume: the request already hashes provider + model + prompt + reference sha; modes have
  different model strings, so keys never collide; the mode is derived from persisted data, so a
  resumed attempt rebuilds the same request (fal resume and cache hits keep working).
- **Tests:** extend `tests/integration/test_product_reference.py` with a montage board of
  3 direct + 1 composite + 2 none: reference provider called 4×, composite provider 1×, plain
  image provider = packshot + 2 none + 1 composite base; no-reference fallback still plain;
  reference reused across keyframe retries (exists).

#### Item 4. Cultural wardrobe lexicon

**Config `config/culture.toml`:**

```toml
[wardrobe.saudi_male]
applies_when = ["ar", "saudi", "gulf", "riyadh", "jeddah", "خليج", "سعود", "الرياض"]
describe = [
  "white Saudi thobe: stiff stand collar or classic collar, concealed placket with no visible buttons, long sleeves with cuffs (cufflinks optional), ankle length, clean pressed cotton",
  "white ghutra or red-and-white shemagh laid flat over the head with a black agal; the drape falls evenly to the shoulders",
]
avoid = [
  "kurta or kameez (mandarin collar with a visible button placket): South Asian, not Saudi",
  "Emirati kandura (tassel at the chest, no collar)",
  "Omani dishdasha (tassel and embroidered collar)",
]

[[wardrobe.saudi_male.lint]]
pattern = "button placket|visible buttons|row of buttons"
message = "a Saudi thobe has a concealed placket with no visible buttons"
[[wardrobe.saudi_male.lint]]
pattern = "kurta|kameez|sherwani|mandarin collar"
message = "that is South Asian; write 'white Saudi thobe with a stiff stand collar'"
[[wardrobe.saudi_male.lint]]
pattern = "kandura|dishdasha|tassel"
message = "Emirati/Omani garment; the character wears a Saudi thobe"
```

**Domain `domain/culture.py`:** `CultureLexicon.load`, `Settings.culture`,
`applies(brief)`, `prompt_block()`, `wardrobe_problems(character) -> list[str]` (regex over
`wardrobe`, `appearance`, `description`; negated clauses dropped like `wordless_label_problems`).
Wired: continuity node `convert` (→ repair) and `continuity` v4 (lexicon in USER);
`prompt_writer` v7 / `prompt_rewrite` v7 (`<wardrobe_lexicon>` when a person is in the shot);
`qa_keyframe` v6 anatomy/wardrobe paragraph gets the same `describe`/`avoid` text through a
`{{ wardrobe_lexicon }}` variable ("(none)" when it does not apply). Not applied to `qa_critic`
(clip) for now: the keyframe gate is where it pays.

**Tests:** `tests/unit/test_culture.py` (lint catches the Step A bible phrasing "mandarin collar
and a visible button placket"; negated "no visible buttons" passes; `applies` for AR, Saudi EN,
and a London brief → false); lexicon text appears in the continuity, prompt_writer and
qa_keyframe requests of a scripted AR run (inspect `provider.requests`).

#### STOP 1 tooling (small, needed for the report)

- `scripts/contact_sheet.py <run>`: PIL grid → `data/runs/<run>/contact_sheet.png`: the
  reference packshot (with its gate score) + every scene's current keyframe in order, captioned
  `S<order> · <vocab> · <identity> · gate <score> ✓/✗ (<tries>)`, rejected stills as small
  thumbnails under each, a grey "refused by the budget" tile for scenes without a keyframe.
- `scripts/run_report.py --decisions`: prints, from state + events: format and why
  (brief.requested_format / physical_product); the three concepts with judge scores and the
  pick; per scene: vocab, identity mode, every gate verdict (score, fail reasons, issues), the
  revision strategies used, cache hits; reference tries and the fallback event if any; budget
  refusals; fal spend from the wallet ledger (`fal_run.report`).

#### Item 5. Peak-moment selection (after STOP 1)

- `media/frames.py`: `motion_profile(video, fps=12) -> list[float]` (one ffmpeg pass:
  `-vf fps=12,scale=160:-1,format=gray -f rawvideo`, numpy mean |Δ| per frame, deterministic);
  `peak_window(profile, fps, clip_s, kept_s) -> (start_s, dur_s)`: the window of `kept_s` with
  the largest motion energy that also contains the global peak frame (so a burst at 2.0 s is
  inside it), clamped to `[0, clip_s − kept_s]`.
- `Scene.use_window: tuple[float, float] | None = None` (derived data, not in `SPEC_FIELDS`;
  cleared wherever `clip` is cleared: `reject`, `mark_stale`, `release_lease` keeps it). Set in
  the scene runner right after a clip is produced (`_succeed`, clip phase: the worker computes it
  from the file, the single writer stores it) for montage scenes; narrative keeps `(0, duration)`.
- Critic: `played_duration` → the window; `sample_frames(..., offset_s)`; dense fractions inside
  the window; the critic prompt's frame labels show absolute clip time.
- Assembly: `normalise_clip(..., start_s=...)` → `-ss start -t dur` (input-side seek, re-encode as
  today). Best-of-N: window computed per candidate before review.
- **Tests:** synthetic clip (`ffmpeg testsrc` 3 s + a white `drawbox` enabled 2.0–2.3 s) →
  `motion_profile` peaks at 2.0 s, the window covers it; assembly of that scene starts at the
  window (first frame mostly white); `sample_times` with offset stays within the window.

#### Item 6. Music + beat-cut assembly (after STOP 1)

- `media/beats.py`: ffmpeg decode to mono 22.05 kHz `s16le` → numpy STFT (1024/512), half-wave
  rectified spectral flux, adaptive-threshold peak picking → onsets; tempo from the autocorrelation
  of the onset envelope (60–180 bpm); a beat grid phase-fitted to the onsets; downbeats every 4.
  `snap_cuts(durations, beats, slack=0.2, hero_index) -> durations'`: each boundary moves to the
  nearest beat within ±slack, the hero shot starts on a downbeat, the end card starts on a beat,
  the total stays within `DURATION_TOLERANCE_S`. Without music: unchanged.
- Assembly: with `music_path`, durations are snapped before trimming (a montage shot's window may
  stretch up to the render length, so the slack is real). Event: "Beat cut: 118 bpm, 9 cuts
  moved ≤ 0.2 s".
- **Tests:** numpy click track at 120 bpm (10 s, WAV) → beats within 20 ms; `snap_cuts` keeps the
  total and moves no cut more than the slack; assembly duration check still passes.

#### Item 8. Best-of-N on key shots (with item 6)

- In `_scene` (clip phase): `n = 2` iff the scene's vocab entry is `key_shot` and
  `budget.remaining_usd − Σ(est. clip of other pending scenes) ≥ 2 × clip_est`; else 1. The
  decision is an event ("Scene 9 hero_push_in: best-of-2 (headroom $1.12)" / "…: 1 take,
  headroom too small"). `--hero` still forces 2 everywhere.
- **Tests:** key shot gets 2 candidates and the others 1 on a generous budget; a tight budget
  gives 1 everywhere.

#### How it plugs into the brain (edits, resume, cache)

- No new stage, transition, delta kind or command. Formats, vocabulary and culture are read by
  nodes through `Settings`; validators feed schema repair; routing is a pure function of persisted
  state (vocab + format + reference).
- Edits: `ScenePatchDraft` unchanged; a shot edit keeps the scene's `vocab`; depth table unchanged;
  rebalance uses the format's kept bounds. `patch_bible`, `correct_scene_flags`, `accept` keep
  working (a corrected flag on a vocab scene is a human override, logged as today).
- Resume/cache: identity mode is deterministic from state, so request hashes are stable across
  processes; `use_window` is derived data recomputed only when a clip is produced.
- Old snapshots: every new field has a default (`vocab=None`, `Storyboard.format="narrative"`,
  `Brief.format="narrative"`, `use_window=None`), so stored runs, the gallery and `docs/media`
  fallbacks load unchanged.

#### ADRs and order of commits

1. `feat(formats): format library, montage grammar, one format per run` + ADR-0018 (also records
   the vocabulary-flags exception to ADR-0010 and the "all concepts share the format" decision).
2. `feat(vocab): shot vocabulary, flags from data, person detail rule`.
3. `feat(media): per-shot identity routing from the vocabulary` + ADR-0019 (with item 4).
4. `feat(culture): wardrobe lexicon + bible lint`.
5. `feat(scripts): contact sheet + decisions report` → **STOP 1**.
6. `feat(media): peak window` · 7. `feat(media): beat cut + key-shot best-of-2` + ADR-0020 →
   **STOP 2**. PROGRESS.md entry at each stop (plus the missing Step A entry).

Upper bounds: items 1–4 + scripts ≈ 4.5 h; 5–6 + 8 ≈ 3 h.

### 3. STOP 1 proof run, precisely

Fully automatic: `--auto` (judge picks), no `--product-ref` flag needed (montage implies the
reference), no spec edits, no flag corrections, no overrides, no `adsmith budget` raise. Stops
before `generate`, so no clip is ever reserved.

```bash
# 0. Read the ledger's settled total first (it is the truth, HANDOFF says 11.30):
uv run python scripts/fal_run.py report            # or: sqlite3 data/account.db "select sum(actual_usd) from spend where state='settled'"
# 1. Ceiling = settled + cap (11.30 + 0.60 = 11.90); keyframe tries bounded to 2; run cap 0.60
ADSMITH_LLM_PROVIDER=anthropic ADSMITH_MEDIA_PROVIDER=fal \
ADSMITH_FAL_VIDEO_MODEL=kling_v3_standard ADSMITH_FAL_BALANCE_USD=11.90 \
ADSMITH_BUDGET_USD=0.60 ADSMITH_KEYFRAME_MAX_TRIES=2 \
uv run adsmith run "سوّ لي إعلان 12 ثانية لعطر رجالي فاخر للشباب، فخم وجريء" --auto --until generate
# 2. Report
uv run python scripts/contact_sheet.py <run>
uv run python scripts/run_report.py <run> --decisions
```

Expected cost (12 s footage → 9 shots at ~1.33 s): 1 packshot $0.025 + ~5 direct × $0.04 +
~1 composite × $0.065 + ~3 plain × $0.025 ≈ **$0.37 first pass**, ≈ **$0.50** with typical
retries, worst case with 2 tries each ≈ $0.71 → the wallet ceiling refuses the last stills and
the sheet shows them as "refused by the budget" tiles. Anthropic spend is separate (≈ $0.3–0.5).

**The report must show:** (a) the contact sheet: reference packshot with its gate score, every
keyframe in order with vocab, identity mode, gate score and tries, rejected stills, budget gaps;
(b) the decisions: resolved format and why; the three concepts with judge scores and the pick;
per shot the vocabulary entry, identity mode, each gate verdict with its reasons, revisions and
cache hits; reference tries and any fallback to plain; budget refusals; fal and Anthropic spend
from the ledgers; (c) a by-eye judgement against references (a) and (b): dark rim-lit look, same
bottle everywhere, physics-led events, ≤ 1 person detail shot, hero last.

### 4. Updates to the existing text above

- Item 1: "≥ 2 montage concepts … `Concept.format`" → one format per run resolved by code;
  `Brief.format` + `Storyboard.format`; `select` stays v5.
- Proof run cost line: footage is 12 s (end card on top), ~9 shots, first pass ≈ $0.37.
- Item 2: `fit` per endpoint → `avoid_on` list.
