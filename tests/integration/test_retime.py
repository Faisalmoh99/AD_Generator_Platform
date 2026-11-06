"""Human retime edit on a finished whole ad: slow a window, trim a later stretch of the same
length, re-assemble into a new file; length, every later timestamp and the audio unchanged."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from adsmith.agent import commands
from adsmith.agent.commands import CommandRejectedError
from adsmith.agent.orchestrator import Orchestrator
from adsmith.domain.ad import RetimeEdit, retime_frames, retime_problems
from adsmith.domain.enums import Stage
from adsmith.media.ffmpeg import retime_graph
from tests.integration.test_multishot_pipeline import REQUEST, photo, runtime, settings

FPS = 24.0


def edit(**over: float | str) -> RetimeEdit:
    base: dict[str, float | str] = {
        "window_start_s": 3.0, "window_end_s": 3.5, "factor": 2.5,
        "trim_start_s": 4.0, "trim_end_s": 4.75, "method": "mci",
    }  # fmt: skip
    return RetimeEdit.model_validate(base | over)


def test_frames_and_a_valid_edit() -> None:
    assert retime_frames(edit(), FPS) == (72, 84, 96, 114, 30)
    assert retime_problems([edit()], fps=FPS, frames=192) == []


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"trim_end_s": 5.0}, "slowing adds 18 frames (0.750 s) but the trim removes 24"),
        ({"trim_start_s": 3.2, "trim_end_s": 3.95}, "trim must start at or after"),
        ({"trim_start_s": 7.5, "trim_end_s": 8.25}, "inside the clip"),
    ],
)
def test_bad_edits_are_refused(over: dict[str, float], message: str) -> None:
    assert any(message in p for p in retime_problems([edit(**over)], fps=FPS, frames=192))


def test_overlapping_edits_are_refused() -> None:
    second = edit(window_start_s=4.2, window_end_s=4.7, trim_start_s=5.0, trim_end_s=5.75)
    assert "edits overlap" in retime_problems([edit(), second], fps=FPS, frames=192)


def test_the_graph_keeps_the_frame_count() -> None:
    e = edit()
    graph, total = retime_graph([(*retime_frames(e, FPS), "mci", 2.5)], frames=192, fps=FPS)
    assert total == 192
    assert "trim=start_frame=72:end_frame=84" in graph and "setpts=2.5*PTS" in graph
    assert "mi_mode=mci" in graph and "trim=start_frame=114:end_frame=192" in graph
    assert "trim=start_frame=84:end_frame=96" in graph  # kept between window and trim
    blend, _ = retime_graph([(*retime_frames(e, FPS), "blend", 2.5)], frames=192, fps=FPS)
    assert "mi_mode=blend" in blend


def _probe(path: Path, *args: str) -> str:
    return subprocess.run(
        ["ffprobe", "-v", "error", *args, str(path)], capture_output=True, text=True, check=True
    ).stdout.strip()


def _audio_md5(path: Path, seconds: float) -> str:
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a", "-t", str(seconds),
         "-f", "md5", "-"],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return out.stdout.strip()


async def test_retime_reassembles_into_a_new_file_with_the_same_length(tmp_path: Path) -> None:
    rt = runtime(settings(tmp_path))
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    first = await orch.run_until_blocked(run.id)
    assert first.stage is Stage.DONE and first.final is not None and first.endcard is not None
    run_dir = rt.storage.artifacts.run_dir(run.id)
    original = (run_dir / "final.mp4").read_bytes()

    commands.retime_ad(rt.storage, run.id, [edit()], fps=FPS, frames=192)
    state = await orch.run_until_blocked(run.id)
    assert state.stage is Stage.DONE and state.final is not None
    v2 = run_dir / "final_v2.mp4"
    assert v2.is_file() and (run_dir / "final.mp4").read_bytes() == original  # original kept
    assert state.final.duration_s == pytest.approx(first.final.duration_s, abs=0.05)
    assert state.endcard == first.endcard  # the same end card
    retimed = rt.storage.artifacts.scratch_dir(run.id) / "assemble" / "retimed.mp4"
    count = "-count_packets"
    assert _probe(retimed, "-select_streams", "v:0", count, "-show_entries",
                  "stream=nb_read_packets", "-of", "csv=p=0") == "192"  # fmt: skip
    assert state.generation is not None and state.generation.artifact is not None
    src = rt.storage.artifacts.resolve(state.generation.artifact)
    assert _audio_md5(retimed, 7.9) == _audio_md5(src, 7.9)  # audio untouched
    summaries = [e.summary for e in rt.storage.events.tail(run.id, limit=3000)]
    assert any(s.startswith("human edit: retime (slow 3-3.5s x2.5 (mci)") for s in summaries)
    assert any("final_v2.mp4" in s and s.startswith("Assembled") for s in summaries)


async def test_retime_needs_a_finished_run(tmp_path: Path) -> None:
    rt = runtime(settings(tmp_path))
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=True, product_photo=photo(tmp_path))
    await orch.run_until_blocked(run.id)
    with pytest.raises(CommandRejectedError, match="no generated whole ad"):
        commands.retime_ad(rt.storage, run.id, [edit()], fps=FPS, frames=192)
    with pytest.raises(CommandRejectedError, match="slowing adds"):
        commands.retime_ad(rt.storage, run.id, [edit(trim_end_s=5.0)], fps=FPS, frames=192)


async def test_a_new_generation_drops_the_old_edits(tmp_path: Path) -> None:
    rt = runtime(settings(tmp_path))
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    await orch.run_until_blocked(run.id)
    commands.retime_ad(rt.storage, run.id, [edit()], fps=FPS, frames=192)
    await orch.run_until_blocked(run.id)
    commands.regenerate_ad(rt.storage, run.id, reason="new roll")
    g = rt.storage.runs.load(run.id).generation
    assert g is not None and g.retimes == [] and g.final_name == "final.mp4"
