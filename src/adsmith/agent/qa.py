"""QA critics.

- ``MockCritic`` (default with mock media) is deterministic: it passes clips unless Chaos Mode
  injected a quality fault into the attempt that produced them, in which case it returns concrete,
  prompt-actionable issues, exactly as the vision critic does.
- ``LLMCritic`` (default with real media) is the vision critic: keyframe + frames sampled at
  15/50/85% of the *played* part of the clip, the shot spec, the scene's bible sections and the
  prompts used → a validated ``QAVerdict``. The LLM fills fields; ``passed`` is computed by code.

Deterministic checks on the critic's output (violations go through the schema-repair loop):
every failed check must be explained by an issue of a matching category, and every cited
``frame_index`` must name an image that was actually sent. ``frames_inspected`` is set by code.
"""

from __future__ import annotations

import asyncio
import base64
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from adsmith.agent.context import NodeContext
from adsmith.agent.nodes.common import concept_block, scene_block, storyboard_block
from adsmith.agent.recovery.errors import PermanentProviderError
from adsmith.config import LLMNode
from adsmith.domain.ad import AdGeneration, AdShotDraft, HumanShot
from adsmith.domain.enums import FailureKind, ScenePhase
from adsmith.domain.generation import Artifact
from adsmith.domain.llm import LLMCallRecord
from adsmith.domain.qa import (
    AdVerdict,
    AdVerdictDraft,
    QAChecks,
    QAIssue,
    QAVerdict,
    QAVerdictDraft,
)
from adsmith.domain.run import RunState
from adsmith.domain.scene import Scene
from adsmith.media.frames import (
    DEFAULT_FRACTIONS,
    DEFAULT_LONG_SIDE,
    DENSE_FRACTIONS,
    image_to_jpeg,
    played_duration,
    sample_frames,
)
from adsmith.providers.llm.base import ImagePart, TextPart

_ALL_OK = QAChecks(
    product_visible=True,
    matches_shot_spec=True,
    lighting_matches=True,
    continuity_ok=True,
    artifacts_free=True,
    brand_safe=True,
)

# A failed check must be explained by at least one issue in one of these categories.
CHECK_CATEGORIES: dict[str, frozenset[str]] = {
    "product_visible": frozenset({"product", "composition", "continuity"}),
    "matches_shot_spec": frozenset(
        {"composition", "motion", "character", "product", "product_detail"}
    ),
    "lighting_matches": frozenset({"lighting"}),
    "continuity_ok": frozenset({"continuity", "character", "product"}),
    "artifacts_free": frozenset({"artifact", "motion", "character", "product"}),
    "brand_safe": frozenset({"safety"}),
    "text_free": frozenset({"artifact", "product", "safety"}),
    "alcohol_free": frozenset({"safety", "product"}),
    "key_action_happens": frozenset({"motion"}),
}


class MockCritic:
    async def review_keyframe(
        self, state: RunState, scene: Scene
    ) -> tuple[QAVerdict, list[LLMCallRecord]]:
        """Mock keyframes are placeholder cards: always pass (no fault model for stills yet)."""
        verdict = QAVerdict(
            score=0.9, checks=_ALL_OK, frames_inspected=1, threshold=state.config.qa_threshold
        )
        return verdict, []

    async def review_product_reference(
        self, state: RunState, image: Artifact, prompt: str
    ) -> tuple[QAVerdict, list[LLMCallRecord]]:
        """Mock packshots are placeholder cards: always pass."""
        verdict = QAVerdict(
            score=0.9, checks=_ALL_OK, frames_inspected=1, threshold=state.config.qa_threshold
        )
        return verdict, []

    async def review_ad(self, state: RunState) -> tuple[QAVerdict, list[LLMCallRecord]]:
        """Mock media cannot be judged: the whole-ad generation passes (ADR-0023)."""
        verdict = QAVerdict(
            score=0.9, checks=_ALL_OK, frames_inspected=1, threshold=state.config.qa_threshold
        )
        return verdict, []

    async def review(self, state: RunState, scene: Scene) -> tuple[QAVerdict, list[LLMCallRecord]]:
        # The attempt that produced the clip under review (best-of-N reviews several candidates).
        clip = next(
            (a for a in reversed(scene.attempts) if scene.clip and a.artifact == scene.clip),
            scene.last_attempt(ScenePhase.CLIP),
        )
        threshold = state.config.qa_threshold
        faulty = clip is not None and clip.injected_fault is FailureKind.QUALITY_REJECTED
        if not faulty:
            return QAVerdict(score=0.9, checks=_ALL_OK, frames_inspected=3, threshold=threshold), []
        issue = QAIssue(
            category="motion",
            severity="major",
            observation="Camera movement is too fast and the subject smears in the middle frames.",
            fix="Slow the camera move to a gentle, steady pace and hold the subject sharp.",
            frame_index=1,
        )
        verdict = QAVerdict(
            score=0.45,
            checks=_ALL_OK.model_copy(update={"artifacts_free": False}),
            issues=[issue],
            revised_prompt_hint="Halve the camera speed; keep the final second still.",
            frames_inspected=3,
            threshold=threshold,
        )
        return verdict, []


# Actions that last a fraction of a second: three sampled frames can miss them entirely.
_BRIEF_EVENT = re.compile(
    r"\b(spray\w*|spritz\w*|splash\w*|burst\w*|flash\w*|snap\w*|pour\w*|drops?|"
    r"pops?|popping|flick\w*|toss\w*|blink\w*|press(es|ed)?|squeez\w*|sparks?)\b",
    re.IGNORECASE,
)


def needs_dense_sampling(state: RunState, scene: Scene) -> bool:
    """A key-action check applies: a single-take ad (the action is the whole ad) or a shot whose
    subject_action is a brief event."""
    single_take = state.storyboard is not None and len(state.storyboard.scenes) == 1
    return single_take or bool(_BRIEF_EVENT.search(scene.shot.subject_action))


def check_verdict(draft: QAVerdictDraft, *, n_images: int) -> list[str]:
    """Deterministic consistency rules for a critic verdict. Returns the problems found."""
    problems: list[str] = []
    for check, ok in draft.checks.model_dump().items():
        if ok:
            continue
        allowed = CHECK_CATEGORIES[check]
        if not any(i.category in allowed for i in draft.issues):
            problems.append(
                f"checks.{check} is false but no issue explains it "
                f"(add an issue with category in {sorted(allowed)})"
            )
    for n, issue in enumerate(draft.issues):
        if issue.frame_index is not None and issue.frame_index >= n_images:
            problems.append(
                f"issues.{n}.frame_index={issue.frame_index} but only images 0..{n_images - 1} "
                "were provided"
            )
    return problems


@dataclass(frozen=True, slots=True)
class LLMCritic:
    ctx: NodeContext
    template_version: int | None = None  # None = latest (calibration compares versions)
    fractions: tuple[float, ...] = DEFAULT_FRACTIONS
    long_side: int = DEFAULT_LONG_SIDE

    async def review(self, state: RunState, scene: Scene) -> tuple[QAVerdict, list[LLMCallRecord]]:
        if scene.clip is None or scene.keyframe is None or scene.prompt is None:
            raise PermanentProviderError(f"scene {scene.order} has no clip/keyframe to review")
        assert state.bible is not None
        arts = self.ctx.artifacts
        start, kept = scene.kept_window
        played = played_duration(kept, None if start else scene.clip.duration_s)
        keyframe_jpeg = await asyncio.to_thread(
            image_to_jpeg, arts.resolve(scene.keyframe), long_side=self.long_side
        )
        fractions = self.fractions
        if fractions == DEFAULT_FRACTIONS and needs_dense_sampling(state, scene):
            fractions = DENSE_FRACTIONS  # explicit calibration fractions are left alone
        frames = await sample_frames(
            arts.resolve(scene.clip),
            played,
            fractions=fractions,
            long_side=self.long_side,
            offset_s=start,
        )
        parts: list[ImagePart | TextPart] = [
            TextPart("Image 0: keyframe (the still the clip was animated from)"),
            ImagePart("image/jpeg", base64.b64encode(keyframe_jpeg).decode("ascii")),
        ]
        labels = ["0: keyframe"]
        for n, f in enumerate(frames, start=1):
            label = f"{n}: clip frame at {f.time_s:.2f}s ({f.fraction:.0%} of the played window)"
            parts += [TextPart(f"Image {label}"), ImagePart("image/jpeg", f.b64)]
            labels.append(label)
        n_images = len(frames) + 1

        rendered = self.ctx.prompts.get("qa_critic", self.template_version).render(
            scene=scene_block(scene),
            scene_bible=state.bible.prompt_block(scene.uses_bible_sections()),
            keyframe_prompt=scene.prompt.keyframe_prompt,
            motion_prompt=scene.prompt.motion_prompt,
            negative_prompt=scene.prompt.negative_prompt or "(none)",
            image_list="\n".join(f"- image {label}" for label in labels),
            product_checklist=_product_checklist(state, scene, self.ctx),
            played_s=f"{played:g}",
            composition_rule=composition_rule(self.ctx, scene),
        )
        threshold = state.config.qa_threshold
        single_take = state.storyboard is not None and len(state.storyboard.scenes) == 1
        hard = composition_is_spec(self.ctx, scene)

        def convert(d: QAVerdictDraft) -> QAVerdict:
            problems = check_verdict(d, n_images=n_images)
            if problems:
                raise ValueError("; ".join(problems))
            fixed = d.model_copy(update={"frames_inspected": n_images})
            return QAVerdict.from_draft(
                fixed,
                threshold=threshold,
                keyframe_approved=scene.keyframe_approved,
                single_take=single_take,
                composition_hard=hard,
            )

        res = await self.ctx.llm.call_and_convert(
            LLMNode.QA_CRITIC, rendered, QAVerdictDraft, convert, images=parts
        )
        return res.value, res.calls

    async def review_ad(self, state: RunState) -> tuple[QAVerdict, list[LLMCallRecord]]:
        """The whole-ad critic (ADR-0025): the run's generated video, frames per planned shot,
        the real product photo as image 0."""
        g = state.generation
        if g is None or g.artifact is None:
            raise PermanentProviderError("there is no generated ad to review")
        video = self.ctx.artifacts.resolve(g.artifact)
        video_s = g.artifact.duration_s or float(g.duration_s)
        reference = self.ctx.artifacts.resolve(state.product_photo) if state.product_photo else None
        verdict, calls = await self.review_video(
            state,
            video=video,
            shots=ad_shot_windows(g, video_s),
            reference=reference,
            prompt_text=g.prompt_text,
            duration_s=video_s,
        )
        return verdict, calls

    async def review_video(
        self,
        state: RunState,
        *,
        video: Path,
        shots: Sequence[ShotWindow],
        reference: Path | None,
        prompt_text: str,
        duration_s: float,
    ) -> tuple[AdVerdict, list[LLMCallRecord]]:
        """Review any finished ad: the hard checks (code decides ``passed``), a note per shot and
        the six rubric scores (code computes the creative total). Also used to calibrate the
        rubric on ads made by the old pipeline (scripts/calibrate_ad_critic.py)."""
        assert state.bible is not None and state.storyboard is not None
        parts: list[ImagePart | TextPart] = []
        labels: list[str] = []
        if reference is not None:
            jpeg = await asyncio.to_thread(image_to_jpeg, reference, long_side=self.long_side)
            parts += [
                TextPart("Image 0: the real product photo (the reference)"),
                ImagePart("image/jpeg", base64.b64encode(jpeg).decode("ascii")),
            ]
            labels.append("0: the real product photo (the reference)")
        for w in shots:
            length = max(0.1, w.end_s - w.start_s)
            frames = await sample_frames(
                video, length, fractions=AD_FRACTIONS, long_side=self.long_side, offset_s=w.start_s
            )
            for f in frames:
                n = len(labels)
                label = f"{n}: shot {w.number} at {f.time_s:.2f}s"
                parts += [TextPart(f"Image {label}"), ImagePart("image/jpeg", f.b64)]
                labels.append(label)
        n_images = len(labels)
        concept = state.concepts.selected if state.concepts else None
        rubric = self.ctx.settings.rubric
        rendered = self.ctx.prompts.get("qa_ad_critic").render(
            concept=concept_block(concept) if concept else "(none)",
            storyboard=storyboard_block(state.storyboard),
            ad_prompt=prompt_text,
            product_checklist="\n".join(f"- {i}" for i in state.bible.product.checklist()),
            rubric=rubric.prompt_block("video"),
            shots="\n".join(
                f"- shot {w.number} ({w.start_s:.1f}-{w.end_s:.1f} s): {w.plan}" for w in shots
            ),
            duration=f"{duration_s:.1f}",
            reference_note="Image 0 is the real product photo: compare the product against it."
            if reference is not None
            else "No product photo was given: compare the product against the checklist.",
            image_list="\n".join(f"- image {label}" for label in labels),
        )
        threshold = state.config.qa_threshold
        creative_threshold = self.ctx.settings.creative_threshold
        numbers = [w.number for w in shots]

        def convert(d: AdVerdictDraft) -> AdVerdict:
            d = ad_product_rule(d)
            problems = check_verdict(d, n_images=n_images)
            got = sorted(s.number for s in d.shots)
            if got != sorted(numbers):
                problems.append(f"give exactly one shot note per planned shot {numbers}, got {got}")
            if problems:
                raise ValueError("; ".join(problems))
            core = QAVerdictDraft.model_validate(
                d.model_dump(include=set(QAVerdictDraft.model_fields))
                | {"frames_inspected": n_images}
            )
            base = QAVerdict.from_draft(core, threshold=threshold)
            return AdVerdict(
                **base.model_dump(),
                shots=d.shots,
                rubric=d.rubric,
                creative_note=d.creative_note,
                creative_score=rubric.weighted(d.rubric.model_dump()),
                creative_threshold=creative_threshold,
            )

        res = await self.ctx.llm.call_and_convert(
            LLMNode.QA_CRITIC, rendered, AdVerdictDraft, convert, images=parts
        )
        return res.value, res.calls

    async def review_product_reference(
        self, state: RunState, image: Artifact, prompt: str
    ) -> tuple[QAVerdict, list[LLMCallRecord]]:
        """Gate for the locked product reference (ADR-0003 addendum 3): strict identity, and the
        same hard rule as the keyframe gate (any product mismatch fails, by code)."""
        assert state.bible is not None
        jpeg = await asyncio.to_thread(
            image_to_jpeg, self.ctx.artifacts.resolve(image), long_side=self.long_side
        )
        parts: list[ImagePart | TextPart] = [
            TextPart("Image 0: candidate product reference"),
            ImagePart("image/jpeg", base64.b64encode(jpeg).decode("ascii")),
        ]
        rendered = self.ctx.prompts.get("qa_product_ref").render(
            product_checklist="\n".join(f"- {i}" for i in state.bible.product.checklist()),
            image_prompt=prompt,
        )
        threshold = state.config.qa_threshold

        def convert(d: QAVerdictDraft) -> QAVerdict:
            problems = check_verdict(d, n_images=1)
            if any(i.category == "motion" for i in d.issues):
                problems.append("a still has no motion: use another category")
            if problems:
                raise ValueError("; ".join(problems))
            return QAVerdict.from_draft(gate_draft(d), threshold=threshold)

        res = await self.ctx.llm.call_and_convert(
            LLMNode.QA_CRITIC, rendered, QAVerdictDraft, convert, images=parts
        )
        return res.value, res.calls

    async def review_keyframe(
        self, state: RunState, scene: Scene
    ) -> tuple[QAVerdict, list[LLMCallRecord]]:
        """Keyframe gate: the still alone, before any video spend (ADR-0003 addendum)."""
        if scene.keyframe is None or scene.prompt is None:
            raise PermanentProviderError(f"scene {scene.order} has no keyframe to review")
        assert state.bible is not None
        jpeg = await asyncio.to_thread(
            image_to_jpeg, self.ctx.artifacts.resolve(scene.keyframe), long_side=self.long_side
        )
        parts: list[ImagePart | TextPart] = [
            TextPart("Image 0: keyframe"),
            ImagePart("image/jpeg", base64.b64encode(jpeg).decode("ascii")),
        ]
        rendered = self.ctx.prompts.get("qa_keyframe").render(
            scene=scene_block(scene),
            scene_bible=state.bible.prompt_block(scene.uses_bible_sections()),
            product_checklist=_product_checklist(state, scene, self.ctx),
            keyframe_prompt=scene.prompt.keyframe_prompt,
            wardrobe_lexicon=self.ctx.settings.culture.prompt_block(state.brief)
            if scene.shows_character
            else "(no person in this shot)",
            composition_rule=composition_rule(self.ctx, scene),
        )
        threshold = state.config.qa_threshold
        hard = composition_is_spec(self.ctx, scene)

        def convert(d: QAVerdictDraft) -> QAVerdict:
            problems = check_verdict(d, n_images=1)
            if any(i.category == "motion" for i in d.issues):
                problems.append("a still has no motion: use another category")
            if problems:
                raise ValueError("; ".join(problems))
            return QAVerdict.from_draft(gate_draft(d), threshold=threshold, composition_hard=hard)

        res = await self.ctx.llm.call_and_convert(
            LLMNode.QA_CRITIC, rendered, QAVerdictDraft, convert, images=parts
        )
        return res.value, res.calls


def gate_draft(d: QAVerdictDraft) -> QAVerdictDraft:
    """Keyframe gate rule (hero round): ANY mismatch against the product description is HARD.
    A "product" issue is never minor at the gate, and it fails continuity_ok, whatever the LLM
    said. (The hero keyframe passed at 0.78 with "cap is a black block, not brushed gold" logged
    as a minor product issue; $2.24 of clips then animated the wrong bottle.)"""
    issues = [
        i.model_copy(
            update={
                "frame_index": 0,
                "severity": "major"
                if i.category == "product" and i.severity == "minor"
                else i.severity,
            }
        )
        for i in d.issues
    ]
    checks = d.checks
    if any(i.category == "product" for i in issues):
        checks = checks.model_copy(update={"continuity_ok": False})
    return d.model_copy(update={"frames_inspected": 1, "issues": issues, "checks": checks})


def _product_checklist(state: RunState, scene: Scene, ctx: NodeContext | None = None) -> str:
    assert state.bible is not None
    if not scene.shows_product:
        return "(the product is not in this scene)"
    items = "\n".join(f"- {item}" for item in state.bible.product.checklist())
    entry = ctx.settings.vocabulary.get(scene.vocab) if ctx is not None else None
    if entry is not None and entry.reference_note:
        items += (
            f"\n- BY DESIGN FOR THIS SHOT: {entry.reference_note}. Do not report this "
            "deliberate change as a mismatch."
        )
    if entry is not None and entry.framing == "macro":
        items += (
            "\n- BY DESIGN FOR THIS SHOT: an extreme close-up, so only part of the product is in "
            "frame. Parts outside the frame are not missing; judge only the visible part."
        )
    return items


def composition_is_spec(ctx: NodeContext, scene: Scene) -> bool:
    entry = ctx.settings.vocabulary.get(scene.vocab)
    return entry is not None and entry.composition_is_spec


def composition_rule(ctx: NodeContext, scene: Scene) -> str:
    """Prompt text: whether this shot's composition is soft (default) or its spec (hard)."""
    entry = ctx.settings.vocabulary.get(scene.vocab)
    if entry is None or not entry.composition_is_spec:
        return "Standard: composition problems are soft (minor) for this shot."
    return (
        "HARD FOR THIS SHOT: its composition is the spec. The frame must show: "
        f"{entry.keyframe_hint}. If that event or framing is missing or clearly different, "
        "report a MAJOR 'composition' issue and set matches_shot_spec to false."
    )


# ------------------------------------------------------------------------------ whole ad
AD_FRACTIONS: tuple[float, ...] = (0.2, 0.5, 0.8)  # 3 frames per planned shot


@dataclass(frozen=True, slots=True)
class ShotWindow:
    number: int
    start_s: float
    end_s: float
    plan: str


def ad_shot_windows(g: AdGeneration, video_s: float) -> list[ShotWindow]:
    """The planned shots on the real video's timeline (scaled if the model rendered a slightly
    different length). The model chooses its own cuts, so these are approximate windows."""
    scale = video_s / max(1e-6, float(g.prompt.duration_s))
    # A human-written prompt replaces the plan: its own numbered shots are what was asked for.
    shots: Sequence[AdShotDraft | HumanShot] = (
        g.human_prompt.shots if g.human_prompt else g.prompt.shots
    )
    return [
        ShotWindow(
            s.number, round(s.start_s * scale, 3), round(min(s.end_s * scale, video_s), 3),
            s.action,
        )
        for s in shots
    ]  # fmt: skip


def ad_product_rule(d: AdVerdictDraft) -> AdVerdictDraft:
    """As at the keyframe gate: a salient product mismatch is never minor, and it fails
    continuity_ok, whatever the LLM said (fine details stay 'product_detail', minor)."""
    issues = [
        i.model_copy(update={"severity": "major"})
        if i.category == "product" and i.severity == "minor"
        else i
        for i in d.issues
    ]
    checks = d.checks
    if any(i.category == "product" for i in issues):
        checks = checks.model_copy(update={"continuity_ok": False})
    return d.model_copy(update={"issues": issues, "checks": checks})
