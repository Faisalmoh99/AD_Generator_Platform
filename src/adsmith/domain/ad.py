"""The whole-ad generation unit (multishot format, ADR-0023).

The storyboard's scenes are the numbered shots of ONE prompt; one reference-to-video generation
renders them all, with native sound, from the real product photo. ``AdPrompt`` is that prompt
(written by the ``ad_prompt_writer`` LLM, rendered to text by code). ``AdGeneration`` is the
run-level generation with its own small state machine:

    pending ──estimate──▶ estimated ──approve (human / mock auto)──▶ estimated + approved
       │                     │                                         │ start
       └──fail──▶ failed ◀───┘                                         ▼
                                    generating ──complete──▶ qa_review ──review──▶ approved
                                        │ release (crash)                     └──▶ flagged
                                        └──▶ estimated (approved kept)

``flagged`` = the critic's hard gate failed. Nothing regenerates automatically: a new generation
costs a full ad, so it goes back through the estimate gate by a human command (warn only).
"""

from __future__ import annotations

import itertools
import re
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from adsmith.domain.base import DomainModel, utcnow
from adsmith.domain.generation import Artifact, GenerationAttempt
from adsmith.domain.qa import AdVerdict, QAVerdict
from adsmith.domain.scene import SceneFailure

ShotText = Annotated[str, Field(min_length=1, max_length=400)]
TIMECODE_TOLERANCE_S = 0.25


class AdShotDraft(DomainModel):
    number: int = Field(ge=1, le=12)
    start_s: float = Field(ge=0, le=60)
    end_s: float = Field(gt=0, le=60)
    action: ShotText = Field(description="What the viewer sees happen in this shot, one beat.")
    camera: ShotText = Field(description="Shot size, lens feel and the camera move.")
    lighting: ShotText
    sound: ShotText = Field(description="What is heard in this shot (sound design, music cue).")


class AdPromptDraft(DomainModel):
    """``ad_prompt_writer`` output. English only (generation-facing)."""

    look: Annotated[str, Field(min_length=1, max_length=500)] = Field(
        description="One look for the whole ad: grade, contrast, texture, lighting logic."
    )
    shots: list[AdShotDraft] = Field(min_length=2, max_length=8)
    audio: Annotated[str, Field(min_length=1, max_length=400)] = Field(
        description="The soundtrack arc across the ad (music feel, key sound effects, no voice)."
    )


class AdPrompt(AdPromptDraft):
    duration_s: int = Field(ge=1, le=60)
    aspect_ratio: str
    text: str = Field(min_length=1)  # rendered by code: what the model receives
    template_version: str
    bible_version: int = Field(ge=1)


def render_ad_prompt(draft: AdPromptDraft, *, duration_s: int, aspect_ratio: str) -> str:
    """The text the video model receives. The product-identity and no-text rules are fixed by
    code, so a prompt can never drop them."""
    orient = "vertical" if aspect_ratio in {"9:16", "3:4"} else "horizontal"
    lines = [
        f"{'An' if str(duration_s).startswith('8') or duration_s in (11, 18) else 'A'} "
        f"{duration_s}-second {orient} {aspect_ratio} product film in {len(draft.shots)} shots, "
        "hard cuts between shots.",
        "The product is exactly the one in the reference image: keep its shape, colours, cap and "
        "materials identical in every shot. No text, letters, numbers or logos anywhere.",
        f"Look: {draft.look}",
    ]
    for s in draft.shots:
        lines.append(
            f"Shot {s.number} ({s.start_s:g}-{s.end_s:g} s): {s.action} Camera: {s.camera} "
            f"Lighting: {s.lighting} Sound: {s.sound}"
        )
    lines.append(f"Audio: {draft.audio} No voice-over, no dialogue.")
    return "\n".join(lines)


def ad_prompt_problems(
    draft: AdPromptDraft, durations: list[float], *, duration_s: int, max_chars: int
) -> list[str]:
    """Deterministic checks against the storyboard (→ schema repair): one shot per storyboard
    scene, numbered 1..n, contiguous timecodes from 0 that follow the scene durations, ending at
    the ad's length, and a rendered text within the model's prompt limit."""
    problems: list[str] = []
    n = len(durations)
    if len(draft.shots) != n:
        problems.append(
            f"write exactly {n} shots (one per storyboard scene), not {len(draft.shots)}"
        )
    numbers = [s.number for s in draft.shots]
    if numbers != list(range(1, len(draft.shots) + 1)):
        problems.append(f"number the shots 1..{len(draft.shots)} in order, got {numbers}")
    t = 0.0
    for i, s in enumerate(draft.shots):
        expected_end = t + (durations[i] if i < n else 0.0)
        if abs(s.start_s - t) > TIMECODE_TOLERANCE_S or s.end_s <= s.start_s:
            problems.append(f"shot {s.number} must start at {t:g} s (the previous shot's end)")
        if i < n and abs(s.end_s - expected_end) > TIMECODE_TOLERANCE_S:
            problems.append(
                f"shot {s.number} must end at {expected_end:g} s (its storyboard scene lasts "
                f"{durations[i]:g} s)"
            )
        t = s.end_s
    if draft.shots and abs(draft.shots[-1].end_s - duration_s) > TIMECODE_TOLERANCE_S:
        problems.append(f"the last shot must end at {duration_s} s, the ad's length")
    text = render_ad_prompt(draft, duration_s=duration_s, aspect_ratio="9:16")
    if len(text) > max_chars:
        problems.append(
            f"the rendered prompt is {len(text)} characters; the limit is {max_chars}: shorten "
            "each shot's action, camera, lighting and sound"
        )
    return problems


# ----------------------------------------------------------------------------- human prompt
_SHOT_LINE = re.compile(
    r"^\s*Shot\s+(\d+)\s*\(\s*(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*s\s*\)\s*:\s*(.+)$",
    re.IGNORECASE,
)
_SECTION = re.compile(r"^\s*(Look|Audio)\s*:", re.IGNORECASE)


class HumanShot(DomainModel):
    number: int = Field(ge=1)
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    action: str = Field(min_length=1, max_length=2000)


class HumanPrompt(DomainModel):
    """A prompt written by a human creative director that replaces the model-written one at the
    spend gate. Sent to the video model verbatim; its numbered shots (parsed by code) drive the
    critic's frame windows."""

    text: str = Field(min_length=1)
    shots: list[HumanShot] = Field(min_length=1)
    replaced_at: datetime = Field(default_factory=utcnow)
    source: str = Field(default="", max_length=500)  # e.g. the file it came from


def parse_prompt_shots(text: str) -> list[HumanShot]:
    out = []
    for line in text.splitlines():
        m = _SHOT_LINE.match(line)
        if m:
            out.append(
                HumanShot(
                    number=int(m[1]), start_s=float(m[2]), end_s=float(m[3]), action=m[4].strip()
                )
            )
    return out


def human_prompt_problems(text: str, *, duration_s: int, max_chars: int) -> list[str]:
    """Deterministic checks before a human prompt is accepted: within the model's prompt limit,
    numbered shots 1..n with contiguous timecodes from 0 that end at the ad's length."""
    problems: list[str] = []
    if len(text) > max_chars:
        problems.append(f"the prompt is {len(text)} characters; the limit is {max_chars}")
    shots = parse_prompt_shots(text)
    if not shots:
        problems.append('no numbered shots found (lines like "Shot 1 (0-2 s): ...")')
        return problems
    numbers = [s.number for s in shots]
    if numbers != list(range(1, len(shots) + 1)):
        problems.append(f"shots must be numbered 1..{len(shots)} in order, got {numbers}")
    t = 0.0
    for sh in shots:
        if abs(sh.start_s - t) > TIMECODE_TOLERANCE_S or sh.end_s <= sh.start_s:
            problems.append(f"shot {sh.number} must start at {t:g} s (the previous shot's end)")
        t = sh.end_s
    if abs(shots[-1].end_s - duration_s) > TIMECODE_TOLERANCE_S:
        problems.append(f"the last shot ends at {shots[-1].end_s:g} s, not at {duration_s} s")
    return problems


def _sections(text: str) -> dict[str, str]:
    """Split a prompt into comparable parts: header, Look, Shot N, Audio."""
    parts: dict[str, list[str]] = {}
    key = "header"
    for line in text.splitlines():
        if not line.strip():
            continue
        if m := _SHOT_LINE.match(line):
            key = f"Shot {int(m[1])}"
        elif m2 := _SECTION.match(line):
            key = m2[1].capitalize()
        parts.setdefault(key, []).append(" ".join(line.split()))
    return {k: " ".join(v) for k, v in parts.items()}


def prompt_diff_summary(old: str, new: str) -> str:
    """One line for the trace: which parts changed, were added or removed, and the size."""
    a, b = _sections(old), _sections(new)
    changed = [k for k in b if k in a and a[k] != b[k]]
    added = [k for k in b if k not in a]
    removed = [k for k in a if k not in b]
    same = [k for k in b if k in a and a[k] == b[k]]
    bits = []
    if changed:
        bits.append("changed " + ", ".join(changed))
    if added:
        bits.append("added " + ", ".join(added))
    if removed:
        bits.append("removed " + ", ".join(removed))
    if same:
        bits.append("unchanged " + ", ".join(same))
    bits.append(f"{len(old)} → {len(new)} chars")
    return "; ".join(bits)


# ----------------------------------------------------------------------------- retime edits
RetimeMethod = Literal["blend", "mci"]


class RetimeEdit(DomainModel):
    """A human edit applied at assembly: slow ``window`` by ``factor`` and remove ``trim`` (a
    later, static stretch of the same shot) so the film keeps its length and every timestamp
    after the trim stays where it was. The generation's audio is never touched."""

    window_start_s: float = Field(ge=0)
    window_end_s: float = Field(gt=0)
    factor: float = Field(gt=1, le=4)
    trim_start_s: float = Field(ge=0)
    trim_end_s: float = Field(gt=0)
    method: RetimeMethod = "blend"


def retime_frames(e: RetimeEdit, fps: float) -> tuple[int, int, int, int, int]:
    """(window start, window end, trim start, trim end, frames the slowed window lasts), as frame
    indices on the source (ends exclusive)."""
    ws, we = round(e.window_start_s * fps), round(e.window_end_s * fps)
    ts, te = round(e.trim_start_s * fps), round(e.trim_end_s * fps)
    return ws, we, ts, te, round((we - ws) * e.factor)


def retime_problems(edits: list[RetimeEdit], *, fps: float, frames: int) -> list[str]:
    """Deterministic checks: inside the clip, the trim after its window, no overlaps, and the
    frames added by slowing equal the frames trimmed (the length is unchanged)."""
    problems: list[str] = []
    spans: list[tuple[int, int]] = []
    for i, e in enumerate(edits, start=1):
        ws, we, ts, te, slowed = retime_frames(e, fps)
        if not 0 <= ws < we <= frames or not 0 <= ts < te <= frames:
            problems.append(f"edit {i}: windows must lie inside the clip (0-{frames / fps:.3f} s)")
            continue
        if ts < we:
            problems.append(f"edit {i}: the trim must start at or after the slowed window's end")
        added, removed = slowed - (we - ws), te - ts
        if added != removed:
            problems.append(
                f"edit {i}: slowing adds {added} frames ({added / fps:.3f} s) but the trim removes "
                f"{removed} ({removed / fps:.3f} s): they must match to keep the length"
            )
        spans.append((ws, te))
    spans.sort()
    if any(a[1] > b[0] for a, b in itertools.pairwise(spans)):
        problems.append("edits overlap")
    return problems


AdStatus = Literal[
    "pending", "estimated", "generating", "qa_review", "approved", "flagged", "failed"
]


class IllegalAdTransitionError(ValueError):
    pass


class AdGenerationVersion(DomainModel):
    """A past generation, archived when a human asks for a new one."""

    version: int = Field(ge=1)
    status: AdStatus
    est_usd: float | None = None
    artifact: Artifact | None = None
    qa: AdVerdict | QAVerdict | None = None
    reason: str = Field(max_length=500)
    created_at: datetime = Field(default_factory=utcnow)


class AdGeneration(DomainModel):
    status: AdStatus = "pending"
    prompt: AdPrompt
    model: str = Field(description="Catalog key of the reference-to-video endpoint.")
    provider: str
    duration_s: int = Field(ge=1, le=60)
    resolution: str
    aspect_ratio: str
    generate_audio: bool = True
    # est_usd is the amount the human approves and the wallet reserves (computed + margin);
    # computed_usd is the price itself, price_basis how it was obtained.
    est_usd: float | None = Field(default=None, ge=0)
    computed_usd: float | None = Field(default=None, ge=0)
    price_basis: str | None = Field(default=None, max_length=500)
    estimated_at: datetime | None = None
    approved_usd: float | None = Field(default=None, ge=0)
    approved_by: Literal["user", "auto_mock", "cache"] | None = None
    attempts: list[GenerationAttempt] = Field(default_factory=list)
    artifact: Artifact | None = None
    # AdVerdict from the vision critic; a plain QAVerdict from the mock critic (no rubric: mock
    # media cannot be judged creatively). AdVerdict first: it is the stricter shape.
    qa: AdVerdict | QAVerdict | None = None
    accepted_by_human: str | None = Field(default=None, max_length=500)
    # Set when a human replaced the model-written prompt at the gate: sent verbatim instead.
    human_prompt: HumanPrompt | None = None
    # Human retime edits applied at assembly, and the file name the assembly writes.
    retimes: list[RetimeEdit] = Field(default_factory=list, max_length=8)
    final_name: str = Field(default="final.mp4", pattern=r"^[A-Za-z0-9_.-]+\.mp4$")
    failure: SceneFailure | None = None
    version: int = Field(default=1, ge=1)
    history: list[AdGenerationVersion] = Field(default_factory=list)

    @model_validator(mode="after")
    def _invariants(self) -> Self:
        self.check_invariants()
        return self

    def check_invariants(self) -> None:
        s = self.status
        if (s == "failed") != (self.failure is not None):
            raise ValueError("status 'failed' and a failure record must go together")
        if s in {"qa_review", "approved", "flagged"} and self.artifact is None:
            raise ValueError(f"status {s!r} requires the generated video")
        if s in {"approved", "flagged"} and self.qa is None:
            raise ValueError(f"status {s!r} requires the critic's verdict")
        if s == "generating" and self.approved_usd is None:
            raise ValueError("a generation starts only after its estimate was approved")
        if self.approved_usd is not None and self.est_usd is None:
            raise ValueError("an approval needs an estimate")

    @property
    def prompt_text(self) -> str:
        """What the video model receives: the human's prompt if one replaced the model's."""
        return self.human_prompt.text if self.human_prompt is not None else self.prompt.text

    def replace_prompt(self, human: HumanPrompt) -> None:
        """Human creative direction at the spend gate: the new prompt must be priced again, so
        the estimate and any approval are cleared (back to pending)."""
        self._need("pending", "estimated")
        self.human_prompt = human
        self.status = "pending"
        self.est_usd = self.computed_usd = self.price_basis = None
        self.estimated_at = self.approved_usd = self.approved_by = None
        self.check_invariants()

    @property
    def approved(self) -> bool:
        return self.approved_usd is not None and self.est_usd is not None

    # ----------------------------------------------------------------------- transitions
    def _need(self, *allowed: AdStatus) -> None:
        if self.status not in allowed:
            raise IllegalAdTransitionError(f"ad generation is {self.status}, needs {allowed}")

    def set_estimate(
        self, usd: float, *, computed_usd: float | None = None, basis: str | None = None
    ) -> None:
        """``usd`` is the amount to approve and reserve; ``computed_usd`` the price before the
        safety margin (defaults to ``usd``: no margin)."""
        self._need("pending", "estimated")
        self.est_usd, self.estimated_at, self.status = usd, utcnow(), "estimated"
        self.computed_usd = usd if computed_usd is None else computed_usd
        self.price_basis = basis[:500] if basis else None
        self.approved_usd = self.approved_by = None
        self.check_invariants()

    def approve(self, by: Literal["user", "auto_mock", "cache"]) -> None:
        self._need("estimated")
        self.approved_usd, self.approved_by = self.est_usd, by
        self.check_invariants()

    def start(self, attempt: GenerationAttempt) -> None:
        self._need("estimated")
        if not self.approved:
            raise IllegalAdTransitionError("the estimate has not been approved")
        self.status = "generating"
        self.attempts.append(attempt)
        self.check_invariants()

    def release(self) -> None:
        """generating → estimated (crash recovery: the approval is kept, the job is polled)."""
        self._need("generating")
        self.status = "estimated"
        self.check_invariants()

    def complete(self, artifact: Artifact) -> None:
        self._need("generating", "estimated")
        self.artifact, self.status = artifact, "qa_review"
        self.check_invariants()

    def review(self, verdict: QAVerdict) -> None:
        """The critic's verdict: approved when the hard gate passes, flagged otherwise. Neither
        triggers another generation (warn only)."""
        self._need("qa_review")
        self.qa, self.status = verdict, "approved" if verdict.passed else "flagged"
        self.check_invariants()

    def accept(self, reason: str) -> None:
        """A human accepts a flagged generation (logged; the failing verdict stays)."""
        self._need("flagged")
        self.status, self.accepted_by_human = "approved", reason
        self.check_invariants()

    def fail(self, failure: SceneFailure) -> None:
        self._need("pending", "estimated", "generating", "qa_review")
        self.status, self.failure = "failed", failure
        self.check_invariants()

    def renew(self, reason: str) -> None:
        """Archive this generation and start a new one with the same prompt (a human asked for
        it): new version, so a new idempotency key and a new estimate at the gate."""
        self.history.append(
            AdGenerationVersion(
                version=self.version, status=self.status, est_usd=self.est_usd,
                artifact=self.artifact, qa=self.qa, reason=reason,
            )
        )  # fmt: skip
        self.version += 1
        self.status, self.failure, self.artifact, self.qa = "pending", None, None, None
        self.est_usd = self.estimated_at = self.approved_usd = self.approved_by = None
        self.computed_usd = self.price_basis = None
        self.accepted_by_human = None
        self.retimes, self.final_name = [], "final.mp4"  # edits belong to the old video
        self.check_invariants()
