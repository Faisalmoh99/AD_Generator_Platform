"""Frame sampling for the vision QA critic.

The critic sees the keyframe plus a few frames from the clip. Frames are taken at fixed fractions
of the part of the clip that will actually be **played**: the scene duration, not the rendered
length. Models snap durations upwards (e.g. a 3.5 s scene is rendered as 4 s on Kling) and
assembly trims the tail, so judging the tail would reject clips for frames nobody sees.

Everything is re-encoded to JPEG at a bounded size: vision tokens scale with pixels, and 768 px on
the long side is enough to judge identity, product shape and artifacts.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from adsmith.agent.recovery.errors import PermanentProviderError

DEFAULT_FRACTIONS: tuple[float, ...] = (0.15, 0.5, 0.85)
# Key-action checks (single takes, brief events such as a spray): 8 frames evenly across the
# played window, so a ~0.5 s event cannot fall between samples (hero v2: a spritz at ~2 s of a 3 s
# clip was missed by the 3-frame sampling).
DENSE_FRACTIONS: tuple[float, ...] = tuple((i + 0.5) / 8 for i in range(8))
DEFAULT_LONG_SIDE = 768
JPEG_QUALITY = 85


@dataclass(frozen=True, slots=True)
class SampledFrame:
    time_s: float
    fraction: float
    jpeg: bytes

    @property
    def b64(self) -> str:
        return base64.b64encode(self.jpeg).decode("ascii")


def sample_times(
    played_s: float, fractions: Sequence[float] = DEFAULT_FRACTIONS
) -> list[tuple[float, float]]:
    """``(fraction, time_s)`` pairs inside ``[0, played_s)``."""
    if played_s <= 0:
        raise ValueError("played duration must be positive")
    return [(f, round(min(max(f, 0.0), 0.999) * played_s, 3)) for f in fractions]


def played_duration(scene_duration_s: float, clip_duration_s: float | None) -> float:
    """What the viewer sees: the scene duration, unless the clip is shorter than that."""
    if clip_duration_s is None:
        return scene_duration_s
    return min(scene_duration_s, clip_duration_s)


def _bounded(img: Image.Image, long_side: int) -> Image.Image:
    img = img.convert("RGB")
    scale = long_side / max(img.size)
    if scale < 1:
        img = img.resize(
            (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
            Image.Resampling.LANCZOS,
        )
    return img


def _jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


def image_to_jpeg(path: Path, *, long_side: int = DEFAULT_LONG_SIDE) -> bytes:
    with Image.open(path) as img:
        return _jpeg(_bounded(img, long_side))


async def _frame_at(video: Path, time_s: float, long_side: int) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{time_s:.3f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-c:v",
        "png",
        "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0 or not out:
        detail = err.decode(errors="replace")[-300:] or "no frame decoded"
        raise PermanentProviderError(f"frame sampling at {time_s:.2f}s failed: {detail}")
    with Image.open(io.BytesIO(out)) as img:
        return _jpeg(_bounded(img, long_side))


async def sample_frames(
    video: Path,
    played_s: float,
    *,
    fractions: Sequence[float] = DEFAULT_FRACTIONS,
    long_side: int = DEFAULT_LONG_SIDE,
) -> list[SampledFrame]:
    frames = []
    for fraction, t in sample_times(played_s, fractions):
        frames.append(SampledFrame(t, fraction, await _frame_at(video, t, long_side)))
    return frames


async def probe_video(path: Path) -> tuple[int, int, float]:
    """(width, height, duration_s) of a video file, via ffprobe."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height:format=duration",
        "-of",
        "json",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise PermanentProviderError(f"ffprobe {path.name}: {err.decode(errors='replace')[-300:]}")
    info = json.loads(out)
    stream = info["streams"][0]
    return int(stream["width"]), int(stream["height"]), float(info["format"]["duration"])
