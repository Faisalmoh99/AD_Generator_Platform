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
from dataclasses import dataclass

from adsmith.agent.context import NodeContext
from adsmith.agent.nodes.common import scene_block
from adsmith.agent.recovery.errors import PermanentProviderError
from adsmith.config import LLMNode
from adsmith.domain.enums import FailureKind, ScenePhase
from adsmith.domain.generation import Artifact
from adsmith.domain.llm import LLMCallRecord
from adsmith.domain.qa import QAChecks, QAIssue, QAVerdict, QAVerdictDraft
from adsmith.domain.run import RunState
from adsmith.domain.scene import Scene
from adsmith.media.frames import (
    DEFAULT_FRACTIONS,
    DEFAULT_LONG_SIDE,
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
        played = played_duration(scene.duration_s, scene.clip.duration_s)
        keyframe_jpeg = await asyncio.to_thread(
            image_to_jpeg, arts.resolve(scene.keyframe), long_side=self.long_side
        )
        frames = await sample_frames(
            arts.resolve(scene.clip), played, fractions=self.fractions, long_side=self.long_side
        )
        parts: list[ImagePart | TextPart] = [
            TextPart("Image 0: keyframe (the still the clip was animated from)"),
            ImagePart("image/jpeg", base64.b64encode(keyframe_jpeg).decode("ascii")),
        ]
        labels = ["0: keyframe"]
        for n, f in enumerate(frames, start=1):
            label = f"{n}: clip frame at {f.time_s:.2f}s ({f.fraction:.0%} of the played clip)"
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
            product_checklist=_product_checklist(state, scene),
            played_s=f"{played:g}",
        )
        threshold = state.config.qa_threshold
        single_take = state.storyboard is not None and len(state.storyboard.scenes) == 1

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
            )

        res = await self.ctx.llm.call_and_convert(
            LLMNode.QA_CRITIC, rendered, QAVerdictDraft, convert, images=parts
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
            product_checklist=_product_checklist(state, scene),
            keyframe_prompt=scene.prompt.keyframe_prompt,
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


def _product_checklist(state: RunState, scene: Scene) -> str:
    assert state.bible is not None
    if not scene.shows_product:
        return "(the product is not in this scene)"
    return "\n".join(f"- {item}" for item in state.bible.product.checklist())
