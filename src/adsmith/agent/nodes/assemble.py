"""assemble: approved clips → one MP4 (each trimmed to its scene duration, normalised, joined).

Failed scenes are skipped, so the run ends ``done_partial`` with a watchable ad and a clear list of
what failed. End card, music and crossfades arrive in M8.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from adsmith.agent.context import NodeContext
from adsmith.agent.deltas import SetFinal
from adsmith.agent.nodes.base import NodeResult
from adsmith.agent.nodes.common import decision
from adsmith.agent.recovery.errors import PermanentProviderError
from adsmith.domain.enums import SceneStatus, Stage
from adsmith.domain.run import RunState
from adsmith.providers.media.base import frame_size
from adsmith.providers.media.mock import FPS


async def _ffmpeg(*args: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise PermanentProviderError(f"ffmpeg: {err.decode(errors='replace')[-400:]}")


async def assemble(state: RunState, ctx: NodeContext) -> NodeResult:
    assert state.storyboard is not None and state.brief is not None
    approved = [s for s in state.storyboard.scenes if s.status is SceneStatus.APPROVED and s.clip]
    failed = [s.order for s in state.storyboard.scenes if s.status is SceneStatus.FAILED]
    if not approved:
        return NodeResult(
            deltas=[SetFinal(final=None)],
            events=[
                decision(ctx.run_id, Stage.ASSEMBLE, f"Nothing to assemble: scenes {failed} failed")
            ],
        )
    w, h = frame_size(state.brief.aspect_ratio, long_side=ctx.settings.media_long_side)
    work: Path = ctx.artifacts.scratch_dir(ctx.run_id) / "assemble"
    await asyncio.to_thread(work.mkdir, parents=True, exist_ok=True)
    parts = []
    for s in approved:
        assert s.clip is not None
        seg = work / f"seg_{s.order}.mp4"
        await _ffmpeg(
            "-i",
            str(ctx.artifacts.resolve(s.clip)),
            "-t",
            f"{s.duration_s:.3f}",
            "-vf",
            f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps={FPS},format=yuv420p",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            str(seg),
        )
        parts.append(seg)
    listing = work / "concat.txt"
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts))
    out = ctx.artifacts.run_dir(ctx.run_id) / "final.mp4"
    await _ffmpeg("-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(out))
    duration = round(sum(s.duration_s for s in approved), 3)
    final = ctx.artifacts.ingest(
        out, kind="video", provider="ffmpeg", width=w, height=h, duration_s=duration
    )
    summary = f"Assembled {len(approved)} scene(s), {duration:g}s → final.mp4"
    if failed:
        summary += f"; skipped failed scene(s) {failed}"
    return NodeResult(
        deltas=[SetFinal(final=final)], events=[decision(ctx.run_id, Stage.ASSEMBLE, summary)]
    )
