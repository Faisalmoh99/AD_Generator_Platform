"""Media provider interfaces: text-to-image keyframes and image-to-video clips.

Providers do one call and return a local file plus metadata. They raise only classified
``AgentError``s (transient / content policy / permanent). Caching, budget, chaos and retries are
the agent's job (``agent/media.py``, ``agent/scenes.py``), so every provider behaves the same.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from adsmith.domain.enums import ScenePhase
from adsmith.domain.generation import RemoteRequest

if TYPE_CHECKING:
    from adsmith.config import CreditPricing, HiggsfieldTokenPricing


# Prefix of the message of a paid submit whose answer was lost on a provider without an
# idempotency key (Kie, ADR-0027). The generation runner never submits that version again.
AMBIGUOUS_SUBMIT = "ambiguous submit"


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
class RemoteControl:
    """Crash-safety hooks for paid remote jobs: ``resume`` = a request submitted by an earlier,
    interrupted attempt (poll it, do not resubmit); ``on_submitted`` = persist a new submission."""

    resume: RemoteRequest | None = None
    on_submitted: Callable[[RemoteRequest], None] | None = None


@dataclass(frozen=True, slots=True)
class ImageJob:
    prompt: str
    negative_prompt: str
    width: int
    height: int
    seed: int
    label: MediaLabel
    remote: RemoteControl | None = None
    # Reference-conditioned generation: the locked product still the model must keep unchanged.
    reference_path: Path | None = None
    # Composite mode: the plain scene keyframe the reference product is swapped into.
    base_path: Path | None = None


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
    remote: RemoteControl | None = None
    variant: int = 0  # best-of-N candidate index: a distinct paid job for the same request


@dataclass(frozen=True, slots=True)
class MediaOutput:
    path: Path
    kind: Literal["image", "video", "audio"]
    cost_usd: float
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None
    remote_url: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class ImageProvider(Protocol):
    name: str
    model: str

    def estimate_usd(self, width: int, height: int) -> float: ...

    async def generate_image(self, job: ImageJob, out_dir: Path) -> MediaOutput: ...


class MusicProvider(Protocol):
    """Text → one instrumental track (STOP 2). ``track_s`` is what the model renders."""

    name: str
    model: str
    usd_per_track: float
    track_s: float

    async def generate_music(
        self, prompt: str, seed: int, out_dir: Path, *, ref: str
    ) -> MediaOutput: ...


class VideoProvider(Protocol):
    name: str
    model: str
    usd_per_second: float
    capabilities: VideoCapabilities

    async def image_to_video(self, job: VideoJob, out_dir: Path) -> MediaOutput: ...


@dataclass(frozen=True, slots=True)
class ReferenceVideoJob:
    """One whole ad from reference images (Seedance reference-to-video, Phase A): the prompt
    carries the numbered shots; ``image_urls`` are PUBLIC urls made by the provider's own upload;
    ``est_usd`` is the provider's own estimate for exactly this body, approved by the human and
    reserved in the wallet before the submit. A job without an estimate is never submitted."""

    prompt: str
    image_urls: tuple[str, ...]
    duration_s: int
    resolution: str
    aspect_ratio: str
    generate_audio: bool
    label: MediaLabel
    est_usd: float | None = None
    remote: RemoteControl | None = None
    variant: int = 0  # best-of-N candidate index (the model has no seed: the key differs)
    # The price before the safety margin (``est_usd`` is what is reserved). The wallet is settled
    # at this amount after a successful generation; None = settle at ``est_usd``.
    computed_usd: float | None = None


@dataclass(frozen=True, slots=True)
class PriceQuote:
    """What one generation will cost. ``computed_usd`` is the price itself (the account's own
    number, or the token formula); ``reserved_usd`` = computed + the safety margin, the amount the
    gate shows for approval and the wallet reserves. ``basis`` explains the number."""

    computed_usd: float
    reserved_usd: float
    source: Literal["estimate_api", "token_formula", "credit_formula", "mock"]
    basis: str
    width: int | None = None
    height: int | None = None


def token_quote(
    pricing: HiggsfieldTokenPricing,
    *,
    resolution: str,
    aspect_ratio: str,
    duration_s: int,
    source: Literal["token_formula", "mock"] = "token_formula",
) -> PriceQuote:
    """Price a generation from the token formula at the ASSUMED frame size, plus the margin."""
    w, h = pricing.frame(resolution, aspect_ratio)
    tokens = pricing.tokens(w, h, duration_s)
    usd = round(pricing.usd(resolution, w, h, duration_s), 6)
    rate = pricing.usd_per_1k_tokens[resolution]
    basis = (
        f"{w}x{h} (assumed) x {duration_s} s x {pricing.fps} fps / {pricing.tokens_divisor} = "
        f"{tokens:,} tokens x ${rate:g}/1k = ${usd:.3f}; +{pricing.margin:.0%} margin"
    )
    return PriceQuote(
        computed_usd=usd,
        reserved_usd=round(usd * (1 + pricing.margin), 6),
        source=source,
        basis=basis,
        width=w,
        height=h,
    )


def credit_quote(
    pricing: CreditPricing,
    *,
    resolution: str,
    duration_s: int,
    source: Literal["credit_formula", "mock"] = "credit_formula",
) -> PriceQuote:
    """Price a generation from a published per-second credit rate (Kie.ai, ADR-0027)."""
    n_credits = pricing.credits(resolution, duration_s)
    usd = pricing.usd(resolution, duration_s)
    rate = pricing.credits_per_second[resolution]
    basis = (
        f"{duration_s} s x {rate:g} credits/s ({resolution}) = {n_credits:g} credits x "
        f"${pricing.usd_per_credit:g} = ${usd:.3f}; +{pricing.margin:.0%} margin"
    )
    return PriceQuote(
        computed_usd=usd,
        reserved_usd=round(usd * (1 + pricing.margin), 6),
        source=source,
        basis=basis,
    )


class ReferenceVideoProvider(Protocol):
    """Reference images + one prompt → one multi-shot video with native audio."""

    name: str
    model: str
    max_prompt_chars: int

    async def reference_url(self, path: Path) -> str:
        """A url the model can read for a local reference image (public for real providers)."""
        ...

    async def quote(self, job: ReferenceVideoJob) -> PriceQuote:
        """The price of exactly this job (no reservation, nothing generated)."""
        ...

    async def reference_to_video(self, job: ReferenceVideoJob, out_dir: Path) -> MediaOutput: ...


def frame_size(aspect_ratio: str, *, long_side: int) -> tuple[int, int]:
    short = int(long_side * 9 / 16) // 2 * 2
    return {"9:16": (short, long_side), "16:9": (long_side, short), "1:1": (short, short)}[
        aspect_ratio
    ]
