"""Mock media providers (ADR-0008): real files, zero keys, deterministic.

- ``MockImageProvider``: a Pillow storyboard card per scene (palette from the bible, scene number,
  shot type, camera move, wrapped prompt excerpt).
- ``MockVideoProvider``: ffmpeg animates the keyframe with a camera move that matches the shot
  spec (push-in = zoom in, pan/truck = slide, static = hold) for exactly the requested duration,
  with a "S3 · v2 · attempt 1" badge composited on top. (This ffmpeg build has no drawtext, so all
  text is rendered with Pillow.)
- A deterministic **fail plan** reproduces any recovery path in tests and demos:
  ``"2:clip:permanent,1:keyframe:transient*3"`` = scene 2's clips always fail permanently, and
  scene 1's first 3 keyframe calls fail transiently. Random faults are Chaos Mode's job.
- Costs are *simulated* at the configured fal prices, so the budget guard behaves as it would
  with real money.
"""

from __future__ import annotations

import asyncio
import hashlib
import textwrap
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from adsmith.agent.recovery.errors import (
    AgentError,
    ContentPolicyError,
    PermanentProviderError,
    RateLimitedError,
    TransientProviderError,
)
from adsmith.domain.enums import ScenePhase
from adsmith.providers.media.base import (
    ImageJob,
    MediaLabel,
    MediaOutput,
    VideoCapabilities,
    VideoJob,
)

Sleep = Callable[[float], Awaitable[None]]
FPS = 24


# ------------------------------------------------------------------------------ fail plan
@dataclass(frozen=True, slots=True)
class FailRule:
    scene_order: int
    phase: ScenePhase
    kind: str  # transient | rate_limited | policy | permanent
    times: int | None  # None = always


def parse_fail_plan(spec: str | None) -> list[FailRule]:
    rules: list[FailRule] = []
    for item in filter(None, (s.strip() for s in (spec or "").split(","))):
        head, _, count = item.partition("*")
        order, phase, kind = head.split(":")
        if kind not in {"transient", "rate_limited", "policy", "permanent"}:
            raise ValueError(f"unknown fail kind {kind!r} in {item!r}")
        rules.append(FailRule(int(order), ScenePhase(phase), kind, int(count) if count else None))
    return rules


class FailPlan:
    def __init__(self, rules: list[FailRule]) -> None:
        self.rules = rules
        self._seen: defaultdict[tuple[int, ScenePhase], int] = defaultdict(int)

    def check(self, label: MediaLabel) -> None:
        key = (label.scene_order, label.phase)
        self._seen[key] += 1
        for r in self.rules:
            if (r.scene_order, r.phase) != key:
                continue
            if r.times is not None and self._seen[key] > r.times:
                continue
            raise _error(r.kind, f"mock fail plan: scene {r.scene_order} {r.phase} → {r.kind}")


def _error(kind: str, msg: str) -> AgentError:
    if kind == "transient":
        return TransientProviderError(msg)
    if kind == "rate_limited":
        return RateLimitedError(msg, retry_after_s=0.0)
    if kind == "policy":
        return ContentPolicyError(msg, category="mock")
    return PermanentProviderError(msg)


# ------------------------------------------------------------------------------ rendering
def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except (OSError, TypeError):  # very old Pillow without bundled FreeType
        return ImageFont.load_default()


def _hex(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _palette(label: MediaLabel, seed: str) -> list[tuple[int, int, int]]:
    if label.palette:
        return [_hex(c) for c in label.palette]
    digest = hashlib.sha256(seed.encode()).digest()
    return [(digest[i], digest[i + 1], digest[i + 2]) for i in range(0, 9, 3)]


def render_card(job: ImageJob, path: Path) -> None:
    w, h = job.width, job.height
    colors = _palette(job.label, job.prompt)
    top, bottom = colors[0], colors[1 % len(colors)]
    img = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(img)
    for y in range(h):  # vertical gradient between the first two palette colours
        t = y / max(1, h - 1)
        draw.line(
            [(0, y), (w, y)],
            fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom, strict=True)),
        )
    accent = colors[2 % len(colors)] if len(colors) > 2 else (230, 200, 90)
    draw.rectangle([(0, 0), (w, int(h * 0.18))], fill=(0, 0, 0))
    draw.text(
        (int(w * 0.06), int(h * 0.04)),
        f"S{job.label.scene_order}",
        font=_font(int(h * 0.08)),
        fill=accent,
    )
    draw.text(
        (int(w * 0.32), int(h * 0.06)),
        f"{job.label.shot}\n{job.label.camera}",
        font=_font(int(h * 0.025)),
        fill=(235, 235, 235),
    )
    body = textwrap.fill(job.prompt[:420], width=max(20, w // 15))
    draw.multiline_text(
        (int(w * 0.06), int(h * 0.24)),
        body,
        font=_font(int(h * 0.022)),
        fill=(245, 245, 245),
        spacing=6,
    )
    draw.text(
        (int(w * 0.06), int(h * 0.93)), "MOCK KEYFRAME", font=_font(int(h * 0.02)), fill=accent
    )
    img.save(path)


def _badge(label: MediaLabel, path: Path, width: int) -> None:
    text = (
        f"S{label.scene_order} · v{label.scene_version} · attempt {label.attempt_no} · "
        f"{label.camera}"
    )
    font = _font(max(14, width // 28))
    badge = Image.new("RGBA", (int(width * 0.9), max(30, width // 14)), (0, 0, 0, 170))
    ImageDraw.Draw(badge).text((10, 6), text, font=font, fill=(255, 255, 255, 255))
    badge.save(path)


def _zoompan(camera: str, frames: int, w: int, h: int) -> str:
    step = 0.25 / max(1, frames)
    center = "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    if camera in {"dolly_in", "push_in", "rack_focus", "crane", "pedestal"}:
        z = f"z='min(1+{step}*on,1.25)':{center}"
    elif camera in {"dolly_out", "pull_out"}:
        z = f"z='max(1.25-{step}*on,1)':{center}"
    elif camera in {"pan", "truck", "gimbal_follow", "orbit", "handheld"}:
        z = f"z='1.15':x='(iw-iw/zoom)*on/{frames}':y='ih/2-(ih/zoom/2)'"
    elif camera == "tilt":
        z = f"z='1.15':x='iw/2-(iw/zoom/2)':y='(ih-ih/zoom)*(1-on/{frames})'"
    else:
        z = f"z='1.02':{center}"
    return f"zoompan={z}:d={frames}:s={w}x{h}:fps={FPS}"


async def _run(*args: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise PermanentProviderError(f"ffmpeg failed: {err.decode(errors='replace')[-500:]}")


# ------------------------------------------------------------------------------ providers
class MockImageProvider:
    name = "mock"
    model = "mock-keyframe"

    def __init__(
        self, *, usd_per_image: float, latency_s: float, sleep: Sleep, fail_plan: FailPlan
    ) -> None:
        self.usd_per_image = usd_per_image
        self._latency, self._sleep, self._plan = latency_s, sleep, fail_plan
        self.calls = 0

    async def generate_image(self, job: ImageJob, out_dir: Path) -> MediaOutput:
        self.calls += 1
        await self._sleep(self._latency)
        self._plan.check(job.label)
        path = (
            out_dir
            / f"kf_s{job.label.scene_order}_v{job.label.scene_version}_a{job.label.attempt_no}.png"
        )
        await asyncio.to_thread(render_card, job, path)
        return MediaOutput(
            path=path, kind="image", cost_usd=self.usd_per_image, width=job.width, height=job.height
        )


class MockVideoProvider:
    name = "mock"
    model = "mock-i2v"

    def __init__(
        self,
        *,
        usd_per_second: float,
        capabilities: VideoCapabilities,
        latency_s: float,
        sleep: Sleep,
        fail_plan: FailPlan,
    ) -> None:
        self.usd_per_second = usd_per_second
        self.capabilities = capabilities
        self._latency, self._sleep, self._plan = latency_s, sleep, fail_plan
        self.calls = 0

    async def image_to_video(self, job: VideoJob, out_dir: Path) -> MediaOutput:
        self.calls += 1
        await self._sleep(self._latency)
        self._plan.check(job.label)
        lbl = job.label
        stem = f"clip_s{lbl.scene_order}_v{lbl.scene_version}_a{lbl.attempt_no}"
        badge, out = out_dir / f"{stem}_badge.png", out_dir / f"{stem}.mp4"
        await asyncio.to_thread(_badge, lbl, badge, job.width)
        frames = max(1, round(job.duration_s * FPS))
        filt = (
            f"[0:v]scale={job.width * 2}:{job.height * 2},"
            f"{_zoompan(lbl.camera, frames, job.width, job.height)}[v];"
            f"[v][1:v]overlay=12:main_h-overlay_h-56,format=yuv420p"
        )
        await _run(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(job.image_path),
            "-i",
            str(badge),
            "-filter_complex",
            filt,
            "-frames:v",
            str(frames),
            "-r",
            str(FPS),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(out),
        )
        badge.unlink(missing_ok=True)
        return MediaOutput(
            path=out,
            kind="video",
            cost_usd=round(self.usd_per_second * job.duration_s, 6),
            width=job.width,
            height=job.height,
            duration_s=frames / FPS,
        )
