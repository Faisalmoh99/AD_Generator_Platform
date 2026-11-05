# ADR-0020: Peak-moment window, beat-cut assembly and key-shot best-of-2

- Status: Accepted (2026-11-05)

## Context
Montage shots keep 1.0-1.5 s of a 3 s Kling render. The hero v2 S1 lesson: the spray happened at
~2 s, so keeping the clip head missed it and the critic judged the wrong frames. The references cut
on the music. Two takes were decided by hand for the shots the ad hangs on.

## Decision
1. **Peak window** (`media/frames.py`): `motion_profile` decodes 12 fps grey 96×96 frames with
   ffmpeg and takes the mean absolute difference per frame (numpy, deterministic); `peak_window`
   picks the kept-length window with the most motion that contains the single strongest frame (the
   event), earliest on ties, the head when there is no usable profile. Stored as
   `Scene.use_window` (derived data: set with the clip by the single writer, cleared with it; not
   part of the spec hash). Montage only; other formats keep the head.
2. The critic samples its frames inside the window; assembly seeks to it (`-ss` before `-i`, re-
   encoded, frame-exact). `prompt_writer` v9 tells montage shots to play the event mid-render.
3. **Beat cut** (`media/beats.py`): spectral-flux onsets → tempo and phase by on-beat minus
   off-beat comb scoring (60-180 bpm, mild prior to 120) → grid beats snapped to onsets, downbeats
   by accent. `snap_cuts` moves each boundary to the nearest beat within ±0.2 s inside the shot's
   bounds (a montage window may grow to the rendered length); the hero start prefers a downbeat;
   the end card starts on a beat. Music is user-supplied only (`ADSMITH_MUSIC_PATH`).
4. **Key-shot takes**: a vocabulary `key_shot` gets best-of-2 clips if, at its turn, the run's
   headroom (total and media cap) covers two takes after one clip for every other shot still
   without one; otherwise one. The decision is a trace event. `--hero` still forces N everywhere.
5. numpy is a new dependency (no audio or ML libraries).

## Alternatives considered
- Letting the critic pick the window from dense frames: an LLM call per candidate window, and the
  critic already misreads brief events. Motion energy is free and reproducible; the critic judges
  the chosen window.
- librosa for beats: a heavy dependency for one onset detector.

## Consequences
+ The S1 failure mode (the moment outside the kept seconds) is handled by code.
+ Cuts land on the beat with no model and no manual timing.
− Motion energy can prefer a camera move over a subtle event; the vocabulary keeps camera moves slow.
− A snapped shot may be up to 0.2 s longer or shorter than planned; the total moves by ≤ 0.2 s.
