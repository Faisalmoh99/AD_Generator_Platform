"""Prompt adapters: provider-agnostic ``ScenePrompt`` → one video model's payload.

Each model differs in duration rules, negative-prompt support, prompt length limits and parameter
names. The adapter owns those differences, so the rest of the agent never branches on the model.
The parameter names for real fal endpoints are re-verified against each model's API page in M11.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from adsmith.domain.scene import ScenePrompt
from adsmith.providers.media.base import VideoCapabilities


@dataclass(frozen=True, slots=True)
class AdaptedVideo:
    prompt: str
    negative_prompt: str
    duration_s: float  # what the model will render (≥ scene duration; trimmed at assembly)
    params: dict[str, Any] = field(default_factory=dict)


class PromptAdapter(Protocol):
    @property
    def name(self) -> str: ...

    def adapt(
        self, prompt: ScenePrompt, duration_s: float, aspect_ratio: str, caps: VideoCapabilities
    ) -> AdaptedVideo: ...


def _truncate(text: str, limit: int) -> str:
    """Cut at a word boundary; adapters front-load what matters, so the tail is safest to lose."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return cut.rstrip(",;:") + "…"


@dataclass(frozen=True, slots=True)
class GenericAdapter:
    """Motion prompt as the main prompt (the keyframe already carries the look)."""

    name: str = "generic"
    fold_negative: bool = False  # for models without a negative-prompt field
    extra: tuple[tuple[str, Any], ...] = ()

    def adapt(
        self, prompt: ScenePrompt, duration_s: float, aspect_ratio: str, caps: VideoCapabilities
    ) -> AdaptedVideo:
        text, negative = prompt.motion_prompt, prompt.negative_prompt
        if self.fold_negative or not caps.supports_negative:
            if negative:
                text = f"{text} Avoid: {negative}."
            negative = ""
        snapped = caps.snap(duration_s)
        return AdaptedVideo(
            prompt=_truncate(text, caps.max_prompt_chars),
            negative_prompt=_truncate(negative, 800),
            duration_s=snapped,
            params={"duration": snapped, "aspect_ratio": aspect_ratio, **dict(self.extra)},
        )


@dataclass(frozen=True, slots=True)
class KlingAdapter:
    """Kling v3 Pro (schema verified M11): duration is a string enum "3".."15", negative prompt
    supported, aspect ratio taken from the start image, audio off (we add our own in M8)."""

    name: str = "kling"
    cfg_scale: float = 0.5

    def adapt(
        self, prompt: ScenePrompt, duration_s: float, aspect_ratio: str, caps: VideoCapabilities
    ) -> AdaptedVideo:
        snapped = caps.snap(duration_s)
        return AdaptedVideo(
            prompt=_truncate(prompt.motion_prompt, min(caps.max_prompt_chars, 2500)),
            negative_prompt=_truncate(prompt.negative_prompt, 800),
            duration_s=snapped,
            params={
                "duration": str(int(snapped)),
                "cfg_scale": self.cfg_scale,
                "generate_audio": False,
            },
        )


@dataclass(frozen=True, slots=True)
class LtxAdapter:
    """LTX-2.3 Fast (schema verified M11): duration int in {6,8,10,...}, no negative-prompt field.

    The negative list is **dropped**, not folded into the prompt: in a diffusion prompt, naming
    "text, watermark, logo" tends to summon them. The keyframe already fixes the look (FLUX dev
    has no negative field either, so the positive keyframe prompt must stand on its own); the
    video model only animates it.
    """

    name: str = "ltx"
    fps: int = 24
    resolution: str = "1080p"

    def adapt(
        self, prompt: ScenePrompt, duration_s: float, aspect_ratio: str, caps: VideoCapabilities
    ) -> AdaptedVideo:
        snapped = caps.snap(duration_s)
        params: dict[str, Any] = {
            "duration": int(snapped),
            "fps": self.fps,
            "resolution": self.resolution,
            "generate_audio": False,
        }
        if aspect_ratio in {"16:9", "9:16"}:
            params["aspect_ratio"] = aspect_ratio
        return AdaptedVideo(
            prompt=_truncate(prompt.motion_prompt, caps.max_prompt_chars),
            negative_prompt="",
            duration_s=snapped,
            params=params,
        )


ADAPTERS: dict[str, PromptAdapter] = {
    "mock": GenericAdapter(name="mock"),
    "kling": KlingAdapter(),
    "ltx": LtxAdapter(),
    "seedance": GenericAdapter(name="seedance", fold_negative=True),
    "veo": GenericAdapter(name="veo"),
}
