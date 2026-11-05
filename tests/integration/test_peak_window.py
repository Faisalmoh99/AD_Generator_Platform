"""PLAN_V2 item 5: each montage shot keeps its peak-motion window, not the clip head."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

from PIL import Image, ImageStat

from adsmith.media import ffmpeg
from adsmith.media.frames import (
    MOTION_FPS,
    _frame_at,
    motion_profile,
    peak_window,
    sample_times,
)
from tests.media_helpers import fast_settings, make_harness

REQUEST = "Make me a 12-second ad for a luxury men's perfume for young men, luxurious and bold."


async def burst_clip(path: Path, *, at: float = 2.0, length: float = 0.3) -> Path:
    """3 s of black with a white flash from ``at`` to ``at + length`` (a spray burst stand-in)."""
    await ffmpeg.run(
        "-f", "lavfi", "-i", "color=c=black:s=160x160:d=3:r=24",
        "-vf", f"drawbox=x=0:y=0:w=160:h=160:color=white:t=fill:"
        f"enable='between(t,{at},{at + length})'",
        "-pix_fmt", "yuv420p", str(path),
    )  # fmt: skip
    return path


async def test_window_covers_a_burst_at_two_seconds(tmp_path: Path) -> None:
    clip = await burst_clip(tmp_path / "burst.mp4")
    profile = await motion_profile(clip)
    peak_t = (profile.index(max(profile)) + 1) / MOTION_FPS
    assert abs(peak_t - 2.0) <= 1 / MOTION_FPS + 1e-6
    start, kept = peak_window(profile, fps=MOTION_FPS, clip_s=3.0, kept_s=1.2)
    assert kept == 1.2 and start <= 2.0 <= start + kept
    assert start > 0.5  # not the head: the head has no event


def test_no_profile_or_short_clip_keeps_the_head() -> None:
    assert peak_window([], fps=12, clip_s=3.0, kept_s=1.2) == (0.0, 1.2)
    assert peak_window([1.0] * 10, fps=12, clip_s=1.0, kept_s=1.2) == (0.0, 1.0)


def test_sampling_stays_inside_the_window() -> None:
    times = [t for _, t in sample_times(1.2, (0.0, 0.5, 0.999), offset_s=1.5)]
    assert all(1.5 <= t < 2.7 for t in times) and times[0] == 1.5


async def test_assembly_honours_the_window(tmp_path: Path) -> None:
    clip = await burst_clip(tmp_path / "burst.mp4")
    seg = await ffmpeg.normalise_clip(
        clip, tmp_path / "seg.mp4", seconds=0.25, width=160, height=160, start_s=2.05
    )
    jpeg = await _frame_at(seg, 0.0, 160)
    head = await _frame_at(clip, 0.0, 160)
    bright = ImageStat.Stat(Image.open(io.BytesIO(jpeg)).convert("L")).mean[0]
    dark = ImageStat.Stat(Image.open(io.BytesIO(head)).convert("L")).mean[0]
    assert bright > 200 and dark < 30


async def test_montage_scenes_get_a_window_and_narrative_scenes_do_not(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path), scenario="montage_en")
    run = h.orch.create_run(REQUEST, interactive=False)
    state = await h.orch.run_until_blocked(run.id)
    assert state.storyboard is not None
    for sc in state.storyboard.scenes:
        assert sc.use_window is not None and sc.use_window[1] == sc.duration_s
        assert sc.clip is not None and sc.clip.duration_s is not None
        assert sc.use_window[0] + sc.use_window[1] <= sc.clip.duration_s + 1e-6
    h2 = make_harness(fast_settings(tmp_path / "n"))
    run2 = h2.orch.create_run(REQUEST, interactive=False)
    state2 = await h2.orch.run_until_blocked(run2.id)
    assert state2.storyboard is not None
    assert all(sc.use_window is None for sc in state2.storyboard.scenes)
    await asyncio.sleep(0)
