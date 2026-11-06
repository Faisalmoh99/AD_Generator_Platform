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


# ------------------------------------------------------------------------ audio-carrying clips
# A whole-ad generation (multishot) brings its own soundtrack. Every part of such an ad carries
# the same audio format (AAC, 48 kHz, stereo), so the parts can be joined with a stream copy.
_AUDIO = ("-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2")


async def has_audio(path: Path) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
        "-of", "csv=p=0", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise PermanentProviderError(f"ffprobe {path.name}: {err.decode(errors='replace')[-300:]}")
    return bool(out.strip())


async def normalise_clip_av(
    src: Path, out: Path, *, seconds: float, width: int, height: int
) -> Path:
    """Like ``normalise_clip`` but the clip's own audio is KEPT (re-encoded to the common format);
    a clip without audio gets a silent track, so every part has the same streams."""
    if await has_audio(src):
        audio_in: list[str] = []
        audio_map = "0:a:0"
    else:
        audio_in = ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        audio_map = "1:a:0"
    await run(
        "-i", str(src), *audio_in, "-t", f"{seconds:.3f}", "-vf", _vf(width, height),
        "-map", "0:v:0", "-map", audio_map, "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "18", *_AUDIO, "-shortest", str(out),
    )  # fmt: skip
    return out


async def with_silence(video: Path, out: Path, *, seconds: float) -> Path:
    """A silent-video part (the end card) with a silent track in the common audio format."""
    await run(
        "-i", str(video), "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", *_AUDIO, "-t", f"{seconds:.3f}",
        str(out),
    )  # fmt: skip
    return out
