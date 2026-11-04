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


ADAPTERS: dict[str, PromptAdapter] = {
    "mock": GenericAdapter(name="mock"),
    "kling": GenericAdapter(name="kling", extra=(("cfg_scale", 0.5),)),
    "seedance": GenericAdapter(name="seedance", fold_negative=True),
    "veo": GenericAdapter(name="veo"),
}
