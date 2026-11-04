"""Media provider interfaces: text-to-image keyframes and image-to-video clips.

Providers do one call and return a local file plus metadata. They raise only classified
``AgentError``s (transient / content policy / permanent). Caching, budget, chaos and retries are
the agent's job (``agent/media.py``, ``agent/scenes.py``), so every provider behaves the same.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from adsmith.domain.enums import ScenePhase


@dataclass(frozen=True, slots=True)
class VideoCapabilities:
    min_duration_s: float
    max_duration_s: float
    allowed_durations_s: tuple[float, ...] | None = None  # discrete only (e.g. Veo 4/6/8)
    supports_negative: bool = True
    max_prompt_chars: int = 2500

    def snap(self, duration_s: float) -> float:
        """Smallest producible duration ≥ the scene's (clips are trimmed at assembly)."""
        if self.allowed_durations_s:
            fits = [d for d in sorted(self.allowed_durations_s) if d >= duration_s - 1e-9]
            return fits[0] if fits else max(self.allowed_durations_s)
        whole = float(math.ceil(duration_s - 1e-9))
        return min(max(whole, self.min_duration_s), self.max_duration_s)


@dataclass(frozen=True, slots=True)
class MediaLabel:
    """Who/what a job is for: used for traces, deterministic chaos and mock rendering."""

    scene_order: int
    scene_version: int
    phase: ScenePhase
    attempt_no: int
    shot: str = ""
    camera: str = "static"
    palette: tuple[str, ...] = ()
    title: str = ""


@dataclass(frozen=True, slots=True)
class ImageJob:
    prompt: str
    negative_prompt: str
    width: int
    height: int
    seed: int
    label: MediaLabel


@dataclass(frozen=True, slots=True)
class VideoJob:
    image_path: Path
    prompt: str
    negative_prompt: str
    duration_s: float
    width: int
    height: int
    params: dict[str, Any]
    label: MediaLabel


@dataclass(frozen=True, slots=True)
class MediaOutput:
    path: Path
    kind: Literal["image", "video"]
    cost_usd: float
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None
    remote_url: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class ImageProvider(Protocol):
    name: str
    model: str
    usd_per_image: float

    async def generate_image(self, job: ImageJob, out_dir: Path) -> MediaOutput: ...


class VideoProvider(Protocol):
    name: str
    model: str
    usd_per_second: float
    capabilities: VideoCapabilities

    async def image_to_video(self, job: VideoJob, out_dir: Path) -> MediaOutput: ...


def frame_size(aspect_ratio: str, *, long_side: int) -> tuple[int, int]:
    short = int(long_side * 9 / 16) // 2 * 2
    return {"9:16": (short, long_side), "16:9": (long_side, short), "1:1": (short, short)}[
        aspect_ratio
    ]
