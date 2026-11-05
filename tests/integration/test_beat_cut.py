"""PLAN_V2 item 6: a mock montage assembled on the beats of user music."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from adsmith.domain.enums import Stage
from adsmith.domain.events import EventType
from tests.media_helpers import fast_settings, make_harness
from tests.unit.test_beats import click_track

REQUEST = "Make me a 12-second ad for a luxury men's perfume for young men, luxurious and bold."


async def test_montage_cuts_land_on_the_beats(tmp_path: Path) -> None:
    music = tmp_path / "music.wav"
    truth = click_track(music, seconds=20.0)  # beats at 0.25 + k * 0.5
    h = make_harness(fast_settings(tmp_path, music_path=music), scenario="montage_en")
    run = h.orch.create_run(REQUEST, interactive=False)
    state = await h.orch.run_until_blocked(run.id)
    assert state.stage is Stage.DONE and state.final is not None
    beat_ev = next(
        e
        for e in h.rt.storage.events.tail(run.id, limit=10_000)
        if e.type is EventType.DECISION and e.summary.startswith("Beat cut:")
    )
    durations = [float(str(d)) for d in beat_ev.payload["durations"]]  # type: ignore[union-attr]
    ends = np.cumsum(durations)
    on_beat = [min(abs(e - t) for t in truth) <= 0.03 for e in ends]
    assert sum(on_beat) >= len(ends) - 1  # every cut that had a beat in reach is on it
    assert abs(ends[-1] - 12.0) <= 0.2  # footage length kept within the slack
    assert state.final.duration_s is not None
    assert abs(state.final.duration_s - (ends[-1] + state.config.endcard_s)) <= 0.15


async def test_without_music_the_planned_durations_are_used(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path, generate_music=False), scenario="montage_en")
    run = h.orch.create_run(REQUEST, interactive=False)
    state = await h.orch.run_until_blocked(run.id)
    assert state.stage is Stage.DONE
    summaries = [e.summary for e in h.rt.storage.events.tail(run.id, limit=10_000)]
    assert not any(s.startswith("Beat cut:") for s in summaries)
