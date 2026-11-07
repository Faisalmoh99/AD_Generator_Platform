"""ADR-0026 phase 6: a whole ad's audio is normalised to -16 LUFS in the one encode of its own
audio (two-pass EBU R128), so the end card still joins by stream copy."""

from __future__ import annotations

from pathlib import Path

import pytest

from adsmith.agent.orchestrator import Orchestrator
from adsmith.domain.enums import Stage
from adsmith.media import ffmpeg
from tests.integration.test_multishot_pipeline import REQUEST, photo, runtime, settings


async def _final(tmp_path: Path, **over: object) -> tuple[Path, list[str]]:
    rt = runtime(settings(tmp_path, **over))
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    state = await orch.run_until_blocked(run.id)
    assert state.stage is Stage.DONE and state.final is not None
    evs = [e.summary for e in rt.storage.events.tail(state.id, limit=3000)]
    return rt.storage.artifacts.resolve(state.final), evs


async def test_the_whole_ad_is_normalised_to_minus_16_lufs(tmp_path: Path) -> None:
    final, evs = await _final(tmp_path)
    seconds = await ffmpeg.duration_of(final)
    measured = await ffmpeg.measure_loudness(final, seconds=seconds, target_lufs=-16.0)
    assert measured is not None
    assert float(measured["input_i"]) == pytest.approx(-16.0, abs=1.0)
    assert float(measured["input_tp"]) <= -1.0
    assert any(e.startswith("Audio normalised to -16 LUFS") for e in evs)


async def test_normalisation_can_be_turned_off(tmp_path: Path) -> None:
    _, evs = await _final(tmp_path, loudness_target_lufs=None)
    assert not any(e.startswith("Audio normalised") for e in evs)


def test_the_second_pass_is_linear_from_the_measurement() -> None:
    f = ffmpeg.loudnorm_filter(
        {"input_i": "-19.7", "input_tp": "-3.1", "input_lra": "10.1",
         "input_thresh": "-30.2", "target_offset": "0.4"},
        -16.0,
    )  # fmt: skip
    assert f.startswith("loudnorm=I=-16.0:TP=-1.5:LRA=11.0:measured_I=-19.7")
    assert f.endswith(":offset=0.4:linear=true")
