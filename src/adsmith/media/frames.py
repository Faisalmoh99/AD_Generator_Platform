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

import numpy as np
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
    played_s: float, fractions: Sequence[float] = DEFAULT_FRACTIONS, offset_s: float = 0.0
) -> list[tuple[float, float]]:
    """``(fraction, time_s)`` pairs inside ``[offset_s, offset_s + played_s)``: the played window
    (the clip head, or the peak window a montage shot keeps)."""
    if played_s <= 0:
        raise ValueError("played duration must be positive")
    return [(f, round(offset_s + min(max(f, 0.0), 0.999) * played_s, 3)) for f in fractions]


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
    offset_s: float = 0.0,
) -> list[SampledFrame]:
    frames = []
    for fraction, t in sample_times(played_s, fractions, offset_s):
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


# ------------------------------------------------------------------------------ peak window
MOTION_FPS = 12
_MOTION_W, _MOTION_H = 96, 96


async def motion_profile(video: Path, *, fps: int = MOTION_FPS) -> list[float]:
    """Mean absolute luma difference between consecutive frames, sampled at ``fps``
    (PLAN_V2 item 5). Deterministic: one ffmpeg decode to small grey frames, then numpy.
    ``profile[i]`` is the motion between frame i and i+1, at time ``(i + 1) / fps``."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(video),
        "-vf", f"fps={fps},scale={_MOTION_W}:{_MOTION_H},format=gray",
        "-f", "rawvideo", "-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise PermanentProviderError(f"motion profile: {err.decode(errors='replace')[-300:]}")
    frames = np.frombuffer(out, dtype=np.uint8)
    n = frames.size // (_MOTION_W * _MOTION_H)
    if n < 2:
        return []
    stack = frames[: n * _MOTION_W * _MOTION_H].reshape(n, -1).astype(np.int16)
    diffs = np.abs(np.diff(stack, axis=0)).mean(axis=1)
    return [round(float(d), 4) for d in diffs]


def peak_window(
    profile: Sequence[float], *, fps: int, clip_s: float, kept_s: float
) -> tuple[float, float]:
    """``(start_s, kept_s)``: the ``kept_s`` window with the most motion that also contains the
    single strongest motion frame (the shot's event: a burst, a splash). Ties → earliest. With no
    usable profile, or a clip no longer than the window, the head is kept (today's behaviour)."""
    kept = min(kept_s, clip_s)
    if not profile or clip_s <= kept + 1e-6:
        return 0.0, round(kept, 3)
    width = max(1, round(kept * fps))
    peak = int(np.argmax(profile))
    last_start = max(0, min(len(profile) - width, int((clip_s - kept) * fps)))
    best, best_energy = 0, -1.0
    for start in range(0, last_start + 1):
        if not start <= peak < start + width:
            continue
        energy = float(sum(profile[start : start + width]))
        if energy > best_energy + 1e-9:
            best, best_energy = start, energy
    if best_energy < 0:  # the peak sits beyond the last full window: keep the last one
        best = last_start
    start_s = min(best / fps, clip_s - kept)
    return round(max(0.0, start_s), 3), round(kept, 3)
