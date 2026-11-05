"""Thin async ffmpeg/ffprobe helpers for assembly. Failures raise ``PermanentProviderError``."""

from __future__ import annotations

import asyncio
from pathlib import Path

from adsmith.agent.recovery.errors import PermanentProviderError
from adsmith.media.frames import probe_video

FPS = 24


async def run(*args: str) -> None:
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


def _vf(width: int, height: int) -> str:
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
        f"setsar=1,fps={FPS},format=yuv420p"
    )


async def normalise_clip(
    src: Path, out: Path, *, seconds: float, width: int, height: int, start_s: float = 0.0
) -> Path:
    """Keep ``seconds`` from ``start_s`` (the head, or a montage shot's peak window) and normalise
    size, SAR, fps and pixel format; drop audio. The seek is before ``-i`` (fast, frame-exact
    because the output is re-encoded)."""
    seek = ["-ss", f"{start_s:.3f}"] if start_s > 0 else []
    await run(
        *seek, "-i", str(src), "-t", f"{seconds:.3f}", "-vf", _vf(width, height), "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", str(out),
    )  # fmt: skip
    return out


async def still_clip(
    image: Path, out: Path, *, seconds: float, width: int, height: int, fade_in_s: float = 0.0
) -> Path:
    vf = _vf(width, height) + (f",fade=t=in:st=0:d={fade_in_s:.2f}" if fade_in_s > 0 else "")
    await run(
        "-loop", "1", "-i", str(image), "-t", f"{seconds:.3f}", "-vf", vf, "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", str(out),
    )  # fmt: skip
    return out


async def crossfade_stills(
    first: Path, second: Path, out: Path, *, seconds: float, fade_s: float, width: int, height: int
) -> Path:
    """A clip that holds ``first`` and dissolves into ``second`` (used for the end card: the
    blurred last frame, then the brand and tagline fading in over it)."""
    fade_s = min(fade_s, seconds / 2)
    await run(
        "-loop", "1", "-t", f"{seconds:.3f}", "-i", str(first),
        "-loop", "1", "-t", f"{seconds:.3f}", "-i", str(second),
        "-filter_complex",
        f"[0:v]{_vf(width, height)}[a];[1:v]{_vf(width, height)}[b];"
        f"[a][b]xfade=transition=fade:duration={fade_s:.2f}:offset=0.15,format=yuv420p[v]",
        "-map", "[v]", "-t", f"{seconds:.3f}", "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", str(out),
    )  # fmt: skip
    return out


async def last_frame(video: Path, out: Path) -> Path:
    await run("-sseof", "-0.08", "-i", str(video), "-frames:v", "1", "-update", "1", str(out))
    return out


async def concat(parts: list[Path], out: Path, workdir: Path) -> Path:
    listing = workdir / "concat.txt"
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts))
    await run("-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(out))
    return out


async def add_music(video: Path, music: Path, out: Path, *, seconds: float) -> Path:
    """User-supplied music only: looped/trimmed to the ad and faded out over the last second."""
    fade_start = max(0.0, seconds - 1.0)
    await run(
        "-i", str(video), "-stream_loop", "-1", "-i", str(music),
        "-map", "0:v", "-map", "1:a", "-c:v", "copy",
        "-af", f"afade=t=out:st={fade_start:.2f}:d=1", "-t", f"{seconds:.3f}",
        "-c:a", "aac", "-b:a", "192k", "-shortest", str(out),
    )  # fmt: skip
    return out


async def duration_of(video: Path) -> float:
    _, _, seconds = await probe_video(video)
    return seconds
