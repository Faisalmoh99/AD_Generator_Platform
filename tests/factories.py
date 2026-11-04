"""Builders for valid domain objects. Tests override only what they care about."""

from __future__ import annotations

import hashlib
from typing import Any

from adsmith.domain.brief import Brief
from adsmith.domain.budget import BudgetLedger
from adsmith.domain.concept import Concept, ConceptSet
from adsmith.domain.continuity import (
    CharacterSpec,
    ColorGrade,
    ContinuityBible,
    LocationSpec,
    ProductSpec,
)
from adsmith.domain.enums import (
    CameraMove,
    Dialect,
    Language,
    ScenePurpose,
    ShotType,
    Stage,
    TimeOfDay,
)
from adsmith.domain.generation import Artifact
from adsmith.domain.qa import QAChecks, QAIssue, QAVerdict
from adsmith.domain.run import RunConfig, RunState
from adsmith.domain.scene import Scene, ScenePrompt, ShotSpec
from adsmith.domain.storyboard import Storyboard
from tests.fixtures.concept_texts import EN_ELEMENTAL, EN_NIGHT_DRIVE, EN_TAILOR


def sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def make_shot(**over: Any) -> ShotSpec:
    base: dict[str, Any] = {
        "shot_type": ShotType.MEDIUM_CLOSE_UP,
        "camera_movement": CameraMove.DOLLY_IN,
        "lens": "85mm, f/1.8, shallow depth of field",
        "lighting": "low-key neon rim light",
        "time_of_day": TimeOfDay.NIGHT,
        "composition": "subject on the left third, city bokeh right",
        "mood": "confident, nocturnal",
        "subject_action": "he glances at his reflection",
        "setting": "car interior, empty city street",
    }
    return ShotSpec(**(base | over))


def make_scene(order: int = 1, **over: Any) -> Scene:
    base: dict[str, Any] = {
        "order": order,
        "purpose": ScenePurpose.BUILD,
        "description": "A young man drives through the city at night.",
        "duration_s": 3.75,
        "shot": make_shot(),
        "shows_character": True,
        "shows_product": False,
    }
    return Scene(**(base | over))


def make_prompt(**over: Any) -> ScenePrompt:
    base: dict[str, Any] = {
        "keyframe_prompt": "medium close-up of a young man in a car at night, neon rim light",
        "motion_prompt": "slow dolly in",
        "negative_prompt": "text, watermark",
        "template_version": "prompt_writer/v1",
        "bible_version": 1,
    }
    return ScenePrompt(**(base | over))


def make_image(seed: str = "kf") -> Artifact:
    return Artifact(kind="image", path=f"cas/{seed}.png", sha256=sha(seed), provider="mock")


def make_video(seed: str = "clip") -> Artifact:
    return Artifact(
        kind="video", path=f"cas/{seed}.mp4", sha256=sha(seed), provider="mock", duration_s=3.75
    )


def make_verdict(score: float = 0.9, threshold: float = 0.7, **over: Any) -> QAVerdict:
    checks = QAChecks(
        product_visible=True,
        matches_shot_spec=True,
        lighting_matches=True,
        continuity_ok=True,
        artifacts_free=True,
        brand_safe=True,
    )
    base: dict[str, Any] = {
        "score": score,
        "checks": checks,
        "issues": [],
        "frames_inspected": 3,
        "threshold": threshold,
    }
    return QAVerdict(**(base | over))


def make_issue(category: str = "lighting", severity: str = "major") -> QAIssue:
    return QAIssue.model_validate(
        {"category": category, "severity": severity, "observation": "x", "fix": "y"}
    )


def make_storyboard(
    durations: tuple[float, ...] = (3.75, 3.75, 3.75, 3.75),
    concept_id: str = "cpt_test",
    target: float = 15.0,
) -> Storyboard:
    scenes = [make_scene(i, duration_s=d) for i, d in enumerate(durations, start=1)]
    return Storyboard(concept_id=concept_id, target_duration_s=target, scenes=scenes)


def make_brief(**over: Any) -> Brief:
    base: dict[str, Any] = {
        "raw_request": "Make me a 15-second ad for a luxury men's perfume, cinematic.",
        "language": Language.EN,
        "dialect": Dialect.NONE,
        "product": "luxury men's perfume",
        "product_category": "fragrance",
        "target_audience": "young men 18-30",
        "tone": ["cinematic", "confident"],
        "visual_style": "cinematic, high contrast",
        "duration_s": 15.0,
        "normalized_request_en": "15s cinematic ad for luxury men's perfume targeting young men",
    }
    return Brief(**(base | over))


def _concept(title: str, signature: str, cid: str) -> Concept:
    sentences = [s.strip() for s in signature.split(".") if s.strip()]
    return Concept(
        id=cid,
        title=title,
        logline=sentences[1],
        narrative_arc=sentences[4:7],
        visual_hook=sentences[2],
        emotional_driver=sentences[3],
        differentiator="Different premise and setting.",
        estimated_scene_count=4,
    )


def make_concept_set(selected: str | None = None) -> ConceptSet:
    concepts = [
        _concept("The Night Drive", EN_NIGHT_DRIVE, "cpt_a"),
        _concept("Elemental", EN_ELEMENTAL, "cpt_b"),
        _concept("The Tailor", EN_TAILOR, "cpt_c"),
    ]
    cs = ConceptSet(concepts=concepts)
    if selected:
        cs.select(selected, "user", None)
    return cs


def make_bible(version: int = 1, **over: Any) -> ContinuityBible:
    base: dict[str, Any] = {
        "version": version,
        "character": CharacterSpec(
            description="young Saudi man, confident",
            age_range="22-28",
            appearance="light stubble, sharp jawline",
            hair="short black fade",
            grooming="well groomed",
            build="athletic",
            wardrobe=["black tailored shirt", "silver watch"],
        ),
        "product": ProductSpec(
            name="NOIR",
            form="faceted rectangular bottle",
            materials="matte black glass, brushed gold cap",
            colors="black glass, deep amber liquid, gold cap",
            branding_text="NOIR",
            branding_style="embossed gold serif",
            hero_angle="three-quarter low angle",
        ),
        "color_grade": ColorGrade(
            palette=["#0B0B0F", "#C9A227", "#1E90FF"], contrast="high", saturation="low"
        ),
        "location": LocationSpec(primary="Riyadh at night"),
        "mood": "confident, nocturnal",
        "lighting": "low-key with neon accents",
        "audio_mood": "deep bass pulse",
        "forbidden": ["competitor brands"],
    }
    return ContinuityBible(**(base | over))


def make_run(**over: Any) -> RunState:
    base: dict[str, Any] = {
        "config": RunConfig(),
        "request": "Make me a 15-second ad for a luxury men's perfume, cinematic.",
        "budget": BudgetLedger(limit_usd=10, max_attempts_per_scene=6, max_total_attempts=40),
    }
    return RunState(**(base | over))


def make_full_run(stage: Stage = Stage.PROMPT) -> RunState:
    """A run with brief, selected concept, prompted storyboard and bible."""
    sb = make_storyboard(concept_id="cpt_a")
    for s in sb.scenes:
        s.set_prompt(make_prompt())
    return make_run(
        stage=stage,
        brief=make_brief(),
        concepts=make_concept_set(selected="cpt_a"),
        storyboard=sb,
        bible=make_bible(),
    )
