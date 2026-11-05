"""PLAN_V2 item 6: beats from music and beat-snapped cuts."""

from __future__ import annotations

import time
import wave
from pathlib import Path

import numpy as np
import pytest

from adsmith.media.beats import SR, BeatGrid, detect_beats, snap_cuts

FIRST, PERIOD = 0.25, 0.5  # 120 bpm


def click_track(path: Path, *, seconds: float = 10.0) -> list[float]:
    """Short noise clicks every 0.5 s from 0.25 s; every 4th click is louder (the downbeat)."""
    rng = np.random.default_rng(7)
    audio = np.zeros(int(seconds * SR), dtype=np.float64)
    onsets = list(np.arange(FIRST, seconds - 0.1, PERIOD))
    for k, t in enumerate(onsets):
        i = int(t * SR)
        burst = rng.uniform(-1, 1, int(0.01 * SR)) * np.exp(-np.linspace(0, 6, int(0.01 * SR)))
        audio[i : i + burst.size] += burst * (0.9 if k % 4 == 0 else 0.45)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    return [round(float(t), 4) for t in onsets]


async def test_click_track_beats_within_20ms(tmp_path: Path) -> None:
    truth = click_track(tmp_path / "click.wav")
    started = time.monotonic()
    grid = await detect_beats(tmp_path / "click.wav")
    assert time.monotonic() - started < 10
    assert grid.bpm == pytest.approx(120, abs=1)
    for t in truth[1:-1]:
        nearest = min(grid.beats, key=lambda b: abs(b - t))
        assert abs(nearest - t) <= 0.02, (t, nearest)
    down = min(grid.downbeats, key=lambda b: abs(b - truth[4]))
    assert abs(down - truth[4]) <= 0.02  # the accented click is a downbeat


def test_cuts_snap_to_beats_within_slack_and_keep_the_total() -> None:
    grid = BeatGrid(120, tuple(np.arange(0.0, 20, 0.5).round(4)), tuple(np.arange(0.0, 20, 2.0)))
    planned = [1.2] * 10
    out = snap_cuts(planned, grid, slack=0.2, hero_index=9, min_s=[1.0] * 10, max_s=[3.0] * 10)
    ends = np.cumsum(out)
    assert all(abs(e * 2 - round(e * 2)) < 1e-6 for e in ends)  # every boundary on a beat
    assert all(abs(a - b) <= 0.2 + 1e-9 for a, b in zip(ends, np.cumsum(planned), strict=True))
    assert abs(ends[-1] - 12.0) <= 0.2  # total footage kept within the slack
    assert all(1.0 - 1e-9 <= d <= 3.0 + 1e-9 for d in out)


def test_the_hero_shot_starts_on_a_downbeat_when_one_is_in_reach() -> None:
    grid = BeatGrid(120, (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0), (0.0, 2.0))
    out = snap_cuts([1.6, 1.4], grid, slack=0.45, hero_index=1)
    assert out[0] == pytest.approx(2.0)  # 1.5 is nearer, but the hero starts on the downbeat


def test_no_beat_in_reach_keeps_the_plan() -> None:
    grid = BeatGrid(60, (0.0, 5.0, 10.0), (0.0,))
    assert snap_cuts([1.2, 1.3], grid, slack=0.2) == [1.2, 1.3]
