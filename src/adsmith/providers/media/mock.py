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
import wave
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from adsmith.agent.recovery.errors import (
    AccountLockedError,
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
        if kind not in {"transient", "rate_limited", "policy", "permanent", "locked"}:
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
    if kind == "locked":
        return AccountLockedError(msg)
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
    """A cinematic *abstract* frame instead of a text dump: the bible palette as a graded
    background, a soft key light, a subject silhouette sized by shot type, vignette and grain.
    No text is drawn (the UI labels scenes; Arabic would need shaping), but every visual
    parameter is derived from the prompt's hash, so an edited prompt visibly yields a different
    keyframe."""
    w, h = job.width, job.height
    colors = _palette(job.label, job.prompt)
    digest = hashlib.sha256(job.prompt.encode()).digest()

    def unit(i: int) -> float:
        return digest[i] / 255

    dark = min(colors, key=sum)
    light = max(colors, key=sum)
    warm = max(colors, key=lambda c: max(c) - min(c))  # most saturated: the key light's tint
    mid = sorted(colors, key=sum)[len(colors) // 2]
    base = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(base)
    for y in range(h):  # graded background: light top -> deep bottom
        t = y / max(1, h - 1)
        c = tuple(int(a * (1 - t) * 0.55 + b * t * 0.35) for a, b in zip(mid, dark, strict=True))
        draw.line([(0, y), (w, y)], fill=c)
    # soft key light (position from the prompt hash)
    glow = Image.new("RGB", (w, h), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    cx, cy, r = int(w * (0.2 + 0.6 * unit(0))), int(h * (0.15 + 0.35 * unit(1))), int(w * 0.55)
    gd.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill=warm)
    glow = glow.filter(ImageFilter.GaussianBlur(radius=w // 5))
    img = Image.blend(base, glow, 0.55)
    # subject silhouette by shot type
    sil = Image.new("L", (w, h), 0)
    sd = ImageDraw.Draw(sil)
    shot = job.label.shot
    sx = int(w * (0.35 + 0.3 * unit(2)))
    if shot in {"close_up", "extreme_close_up", "insert"}:  # an object: tall rounded block
        bw, bh = int(w * 0.34), int(h * 0.42)
        sd.rounded_rectangle(
            [(sx - bw // 2, int(h * 0.36)), (sx + bw // 2, int(h * 0.36) + bh)],
            radius=bw // 8,
            fill=255,
        )
        sd.rectangle([(sx - bw // 6, int(h * 0.28)), (sx + bw // 6, int(h * 0.37))], fill=255)
    elif shot in {"wide", "extreme_wide", "establishing"}:  # a small figure in a big space
        fw, fh = int(w * 0.07), int(h * 0.26)
        sd.ellipse([(sx - fw // 2, int(h * 0.5)), (sx + fw // 2, int(h * 0.5) + fw)], fill=255)
        sd.rounded_rectangle(
            [(sx - fw, int(h * 0.5) + fw), (sx + fw, int(h * 0.5) + fh)], radius=fw // 2, fill=255
        )
    else:  # medium: a mid-sized form
        mw, mh = int(w * 0.26), int(h * 0.38)
        sd.rounded_rectangle(
            [(sx - mw // 2, int(h * 0.42)), (sx + mw // 2, int(h * 0.42) + mh)],
            radius=mw // 3,
            fill=255,
        )
    sil = sil.filter(ImageFilter.GaussianBlur(radius=max(2, w // 90)))
    shade = Image.new("RGB", (w, h), tuple(int(v * 0.25) for v in dark))
    img = Image.composite(shade, img, sil)
    # rim light on the silhouette edge, vignette, grain
    edge = sil.filter(ImageFilter.FIND_EDGES).filter(
        ImageFilter.GaussianBlur(radius=max(1, w // 160))
    )
    img = Image.composite(
        Image.new("RGB", (w, h), light), img, edge.point(lambda v: min(255, v * 3))
    )
    vig = Image.new("L", (w, h), 0)
    ImageDraw.Draw(vig).ellipse([(-w * 0.25, -h * 0.1), (w * 1.25, h * 1.1)], fill=255)
    vig = vig.filter(ImageFilter.GaussianBlur(radius=w // 6))
    img = Image.composite(img, Image.new("RGB", (w, h), (0, 0, 0)), vig)
    noise = Image.effect_noise((w, h), 18).convert("RGB")
    img = Image.blend(img, noise, 0.05)
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

    def estimate_usd(self, width: int, height: int) -> float:
        return self.usd_per_image

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
        debug_badge: bool = False,
    ) -> None:
        self.usd_per_second = usd_per_second
        self.capabilities = capabilities
        self.debug_badge = debug_badge  # "S3 · v2 · attempt 1" burned in, for debugging only
        self._latency, self._sleep, self._plan = latency_s, sleep, fail_plan
        self.calls = 0

    async def image_to_video(self, job: VideoJob, out_dir: Path) -> MediaOutput:
        self.calls += 1
        await self._sleep(self._latency)
        self._plan.check(job.label)
        lbl = job.label
        stem = f"clip_s{lbl.scene_order}_v{lbl.scene_version}_a{lbl.attempt_no}"
        badge, out = out_dir / f"{stem}_badge.png", out_dir / f"{stem}.mp4"
        frames = max(1, round(job.duration_s * FPS))
        # A different prompt (or best-of-N candidate) must give a different clip file, as with a
        # real model, even without the debug badge: tag it with an invisible metadata comment.
        tag = hashlib.sha256(f"{job.prompt}|{job.variant}".encode()).hexdigest()[:16]
        move = (
            f"[0:v]scale={job.width * 2}:{job.height * 2},"
            f"{_zoompan(lbl.camera, frames, job.width, job.height)}"
        )
        inputs = ["-i", str(job.image_path)]
        if self.debug_badge:
            await asyncio.to_thread(_badge, lbl, badge, job.width)
            inputs += ["-i", str(badge)]
            filt = f"{move}[v];[v][1:v]overlay=12:main_h-overlay_h-56,format=yuv420p"
        else:
            filt = f"{move},format=yuv420p"
        await _run(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            *inputs,
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
            "-metadata",
            f"comment=adsmith-mock {tag}",
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


MOCK_MUSIC_BPM = 120.0


def write_click_track(path: Path, *, seconds: float, bpm: float = MOCK_MUSIC_BPM) -> Path:
    """A steady kick-like pulse (decaying 60 Hz bursts on every beat, accented downbeats) under a
    low pad: a deterministic stand-in for a generated track that beat detection can lock onto."""
    sr = 22_050
    t = np.arange(int(seconds * sr)) / sr
    out = 0.05 * np.sin(2 * np.pi * 55.0 * t)
    period = 60.0 / bpm
    for k, start in enumerate(np.arange(0.0, seconds, period)):
        i = int(start * sr)
        n = min(int(0.12 * sr), out.size - i)
        env = np.exp(-np.arange(n) / (0.025 * sr))
        amp = 0.9 if k % 4 == 0 else 0.6
        out[i : i + n] += amp * env * np.sin(2 * np.pi * 60.0 * np.arange(n) / sr)
    pcm = (np.clip(out, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return path


class MockMusicProvider:
    name = "mock"
    model = "mock-music"

    def __init__(self, *, usd_per_track: float, track_s: float, sleep: Sleep) -> None:
        self.usd_per_track, self.track_s, self._sleep = usd_per_track, track_s, sleep
        self.calls = 0
        self.fail_with: AgentError | None = None  # tests: make the next call fail

    async def generate_music(
        self, prompt: str, seed: int, out_dir: Path, *, ref: str
    ) -> MediaOutput:
        self.calls += 1
        await self._sleep(0)
        if self.fail_with is not None:
            raise self.fail_with
        path = await asyncio.to_thread(
            write_click_track, out_dir / f"music_{seed}.wav", seconds=self.track_s
        )
        return MediaOutput(
            path=path, kind="audio", cost_usd=self.usd_per_track, duration_s=self.track_s
        )
