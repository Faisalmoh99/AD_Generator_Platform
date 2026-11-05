# HANDOFF

State of Adsmith on 2026-11-05, for the next session. This file is a set of pointers to where
things live; the details are in the linked files. **Next work: build `docs/PLAN_V2.md`, items
1 → 4, then STOP 1.**

## 1. Current state

- **Milestones done:** M0–M10, M11 (real fal calibration), M12 (README), M13 (demo script); then
  the hero rounds (critic fixes, best-of-N, product reference, composite mode). M12.5 (evals) was
  cut. Log: `docs/PROGRESS.md`. Original plan: `docs/PLAN.md`. Decisions: `docs/adr/0001–0017`
  (many with addenda).
- **Quality gate:** `make check` = ruff + ruff format + mypy --strict + pytest: **490 passed,
  2 skipped** (the 2 are live-API tests, `--live`).
- **Last commit before this handoff:** `3948395` (PLAN_V2). The working tree is clean apart from this
  handoff commit.
- **Works end to end with zero keys:** `make demo` (chaos recoveries → Arabic end card → an edit
  with its blast radius → SIGKILL + resume, ~20 s); `uv run adsmith serve` (web studio: concepts,
  live board, trace filters, budget, edits, Production Pack, Real runs gallery that falls back to
  `docs/media` in a fresh clone). Verified from a fresh clone (PROGRESS, "M13").
- **Works with real models:** Anthropic for every LLM node; fal for FLUX dev keyframes, FLUX
  Kontext (direct and multi), LTX-2.3 Fast, Kling v3 Pro and v3 Standard clips. Crash-safe fal jobs
  (request id persisted at submit, ADR-0015).
- **A web server may be running** on :8765 (`ADSMITH_MOCK_LATENCY_S=1.2 uv run adsmith serve --port
  8765`), started by the user's session. Leave it alone.

### Architecture at a glance (details: README "The brain", `docs/PLAN.md` §1–6)
- **Control flow:** a declared transition table; `next_stage(state)` is pure
  (`agent/transitions.py`). Interactive runs stop at two gates: concept choice and storyboard
  approval (before any clip money).
- **State:** typed Pydantic models (`domain/`); nodes return deltas; one writer per run applies
  them in a write transaction and stores a snapshot (`storage/repo.py`). Scene workers never write
  directly; stale results are vetoed by scene-version checks.
- **Scenes:** pending → generating → (keyframe gate) → clip → QA → approved/failed/stale
  (`domain/scene.py`, `agent/scenes.py`); per-scene isolation; best-of-N; product reference step.
- **Recovery:** taxonomy + bounded strategies (`agent/recovery/`); provider SDK retries off; chaos
  mode for demos; crash-safe resume.
- **Money:** run budget (reserve → settle/release) + cross-run wallet ledger with a hard ceiling.
- **Edits:** parse → deterministic refs → conflict check → pure blast radius (clip / keyframe /
  prompt depth) → regenerate only the affected scenes (`agent/edit/`).

## 2. Real runs (all under `data/`, which is gitignored; committed copies in `docs/media/`)

| Run | What | Output | What went wrong |
|---|---|---|---|
| `run_01M6JA6FP1Q7P2WE70C6GGVQTN` | M11 scene 4, LTX | curtain before/after: `docs/media/curtain_*` | "curtain shadow" rendered as a real curtain veiling the product; the critic caught it, the loop fixed it |
| `run_01M6JF4T2D78QNQJ7ZSE8GA8WR` | 4-scene majlis ad, Kling Pro, $2.93 | `docs/media/majlis_ad_final.mp4` | scene 3 smoke twice → budget guard skipped it; doorway appears mid-clip; bottle shape drifts between scenes |
| `run_01M6JH38ZT2WRDPSQ2GM833F7T` | single take, Kling Pro, $1.43 | `docs/media/single_take_final.mp4` | promised beat (bottle flies) never happened; bottle reads as whisky |
| `run_01M6KJYGMWF7RHQY8B86T5G7JK` | hero 1: spray single take, best-of-2, $2.32 | `docs/media/hero_candidate_*` | both takes rejected: invented label text on a "blank label plate", wrong cap colour passed the gate as "minor" |
| `run_01M6M88JS802K14VDJPHD3400Z` | hero v2 planning try | none | storyboard ignored "three shots" (1 × 9 s); packshot checklist contained "mist" → every packshot failed |
| `run_01M6M9AG90D4ZBB7TE9GHTNZ37` → fork **`run_01M6MAK00158YYA7JN2YBD0VSR`** | hero v2: 3 × 3 s, Kling Standard, locked reference | **`data/runs/run_01M6MAK00158YYA7JN2YBD0VSR/final.mp4`** (11 s) | S1 rejected by the critic (the spray was there); accepted by human override; S2 mist needed 3 re-specs; contact sheet `docs/screenshots/hero_v2_contact_sheet.png` |
| **`run_01M6MCGVC2YFM2RHRCEWJFZZMP`** | Step A fork: S1 as a hand/jawline close-up, composite mode | S1 keyframe only, `docs/screenshots/stepA_s1_composite.png` | **parked at the approval gate, superseded by PLAN_V2; generate no clip for it** |

Run reports: `uv run python scripts/run_report.py <run>`; `uv run adsmith show <run>`, `trace <run>`.

## 3. Lessons from real models (the most important section)

1. **Video models do physics, not acting.** What worked: a still product with light, liquid,
   mist or camera moves around it (S2 mist, S3 hero push-in, the curtain-after clip). What failed:
   people performing an action (spraying at the neck, presenting a bottle), "the bottle flies at
   the lens", any multi-step choreography. The montage format exists for this reason.
2. **Product identity is the #1 failure, and it starts in the keyframe.** Text-to-image re-imagines
   the bottle every time (cap colour and shape, glass form). Fix: one gated reference packshot, then
   every product keyframe generated from it (Kontext). Direct mode keeps the packshot's scale
   (bottle bigger than a hand); composite mode (scene first, then Kontext multi swaps the bottle in)
   keeps the scene's scale. ADR-0003 addenda 3–4.
3. **Any blank surface becomes invented text.** "A blank label plate" turned into "RLYCHADA" in a
   close-up. Wordless products have no label surface at all (continuity lint).
4. **Nouns are taken literally.** "Curtain shadow" → a curtain; "dust motes / particles" → smoke;
   "haze" → fog over the product. Describe light as light. Spray/mist is allowed only when it IS
   the action (shot lint enforces this both ways).
5. **The critic needs a viewer's rule.** It swung three times: too lenient (two majors passed) →
   too strict (10/10 rejected) → hard/soft → strict again (any product mismatch) → **salient vs
   fine** (fail what an audience notices; fine details lower the score). ADR-0016 addenda 3–5.
6. **Three frames miss brief actions; the clip head isn't the moment.** Dense sampling (8 frames)
   now applies to brief events; the peak-window selection is PLAN_V2 item 5.
7. **The critic still misreads.** It saw the S1 spray burst and called it "faint haze", and read a
   tilted bottle as "slimmer". The human override exists for this; the long-term fix is better
   framing (items 2 and 5), not more critic tuning.
8. **The keyframe gate pays for itself** (21 bad stills stopped before animation in the first
   full run, ≈ $5.60). Composition is soft there, so a money shot's composition can pass when it's
   wrong (MORE_TIME 13).
9. **Regional wardrobe drifts.** "Mandarin collar + button placket" in the bible produced a South
   Asian kurta; a full-figure man invites garment errors. Detail framing (hand, cuff, jawline) plus
   a wardrobe lexicon (PLAN_V2 item 4).
10. **The judge plays it safe** (picks producibility over insight) and once ignored the user's shot
    list. The requested shot count is now enforced in code.
11. **FLUX dev ignores some composition requests** (a "horizontal burst from the left" came out as
    a vertical stream with bokeh). FLUX.2 flex is the stronger prompt-follower (MORE_TIME 13).
12. **Kling v3 Standard handles one person and physical events adequately** at $0.084/s; Pro gives
    crisper parallax at $0.112/s (page price). LTX is fast and cheap but veils passing shadows.

## 4. Manual interventions the system can't do yet

Done by hand in the hero rounds; each maps to a PLAN_V2 item (table in PLAN_V2):
- choosing the ad structure and rewriting shots around the model's strengths (items 1–2)
- re-specifying a failing shot (S2 three times; S1 to a detail shot) (item 2; ladder in MORE_TIME 15)
- re-judging and adopting a stored packshot; choosing direct vs composite (item 3; commands
  `adopt_product_reference`, `ADSMITH_PRODUCT_REFERENCE_MODE`)
- patching the bible wardrobe (thobe, not kurta) (item 4; command `patch_bible`)
- rewording specs to dodge flag triggers ("spray", "perfume"); correcting false-positive flags
  (item 2; command `correct_scene_flags`)
- forking to undo an over-broad stale (`runs.fork`, `resume --from-version`)
- the human override on a misjudged clip (`adsmith accept`; keep it; it's a legitimate human gate)
- raising run budgets after planning (`adsmith budget`)

## 5. Money

- **fal balance (dashboard): about $0.035** after STOP 1 ($0.42) and the STOP 1 re-roll ($0.275); it was about $0.73. The user reported $0.90; since then Step A spent $0.17
  (1 Kontext $0.04 + 2 composite $0.065). **No top-ups planned. No clip money until the user tops
  up after seeing the STOP 1 keyframes.**
- **Wallet ledger:** `data/account.db` (`spend` table). Settled total **$11.995** (after STOP 1 and its re-roll).
  `ADSMITH_FAL_BALANCE_USD` in `.env` = **12.03** (= settled + real remaining), so the ledger says
  $0.73 left. For a capped step, set it on the command line to `settled + cap` (STOP 1:
  `11.30 + 0.60 = 11.90`). A run's own cap: `ADSMITH_BUDGET_USD`, or `adsmith budget <run> <usd>`
  (clamped to the wallet headroom).
- **Ledger vs dashboard:** fal's pricing API reports $0.14/s for every Kling tier, but the bill
  follows the page price. Kling v3 Standard is now budgeted at **$0.084/s** (the last round's
  dashboard implied ≈ $0.066/s); Kling v3 Pro is still budgeted at $0.14 (page: $0.112). FLUX dev
  $0.025 and Kontext $0.04 match. Details: `config/models.toml` comments.
- Other knobs: `ADSMITH_KEYFRAME_MAX_TRIES`, `ADSMITH_QA_MAX_REGENERATIONS` (0 = one take),
  `ADSMITH_FAL_VIDEO_MODEL=kling_v3_standard`, `--hero` (best-of-2), `--product-ref`.

## 6. Open items

- **PLAN_V2 items 1–6 and 8** (build order and stops are in the plan).
- `docs/MORE_TIME.md`: 1–8 and 13–15 open (escalation ladder = 15, montage plan = 14,
  money-shot composition = 13).
- PROGRESS.md has no entry yet for Step A (composite mode, `patch_bible`, `correct_scene_flags`,
  the S1 fork); add one when the next milestone is logged.
- The web UI doesn't show the product reference's composite intermediates or a decisions report
  (useful for STOP 1).

## 7. Start here

Reading order: `CLAUDE.md` → this file → `docs/PLAN_V2.md` → `docs/PROGRESS.md` (last three
entries) → `docs/MORE_TIME.md` → key ADRs: **0001** (state machine), **0003** (keyframe-first,
addenda 2–4: best-of-N, reference, composite), **0016** (critic, addenda 3–5), **0017**
(capability profiles, single take).

Key code: `src/adsmith/agent/transitions.py` (control flow), `agent/scenes.py` (scene runner,
gates, best-of-N, product reference), `agent/media.py` (gateway, direct/composite), `agent/qa.py`
(critics, `gate_draft`, dense sampling), `agent/commands.py` (human commands), `config/models.toml`
(models, prices, capability profiles), `prompts/` (versioned templates).

```bash
make check                                    # the gate; never commit red
make demo                                     # zero-key showcase
uv run adsmith serve                          # web studio
uv run adsmith run "<request>" --auto         # mock by default
uv run adsmith show|trace|history <run>
uv run python scripts/run_report.py <run>     # attempts, verdicts, paths
# real: ADSMITH_LLM_PROVIDER=anthropic ADSMITH_MEDIA_PROVIDER=fal ADSMITH_FAL_BALANCE_USD=<settled+cap>
```
