"""Shared rendering of domain objects into prompt text (one canonical form per object)."""

from __future__ import annotations

from adsmith.config import Settings
from adsmith.domain.brief import Brief
from adsmith.domain.concept import Concept
from adsmith.domain.enums import Language, Stage
from adsmith.domain.events import AgentEvent, EventType
from adsmith.domain.scene import Scene
from adsmith.domain.scene_flags import Flag, FlagOverride
from adsmith.domain.storyboard import Storyboard


def language_name(lang: Language) -> str:
    return "Arabic" if lang is Language.AR else "English"


def brief_block(b: Brief) -> str:
    lines = [
        f"Product: {b.product} ({b.product_category})",
        f"Brand: {b.brand_name or 'not specified (do not invent one)'}",
        f"Target audience: {b.target_audience}",
        f"Tone: {', '.join(b.tone)}",
        f"Visual style: {b.visual_style}",
        f"Duration: {b.duration_s:g} seconds",
        f"Aspect ratio: {b.aspect_ratio}",
        f"User language: {language_name(b.language)} (dialect: {b.dialect.value})",
    ]
    if b.must_include:
        lines.append(f"Must include: {'; '.join(b.must_include)}")
    if b.must_avoid:
        lines.append(f"Must avoid: {'; '.join(b.must_avoid)}")
    if b.tagline:
        lines.append(f"Tagline: {b.tagline}")
    if b.cultural_notes:
        lines.append(f"Cultural notes: {'; '.join(b.cultural_notes)}")
    lines.append(f"Original request: {b.raw_request}")
    return "\n".join(lines)


def concept_block(c: Concept, number: int | None = None) -> str:
    head = f"Concept {number}: {c.title}" if number else f"Concept: {c.title}"
    return "\n".join(
        [
            head,
            f"Logline: {c.logline}",
            f"Beats: {' → '.join(c.narrative_arc)}",
            f"Visual hook: {c.visual_hook}",
            f"Insight: {c.insight}",
            f"Emotional driver: {c.emotional_driver}",
            f"Style: {', '.join(c.style_references) or '-'}",
        ]
    )


def decision(run_id: str, stage: Stage, summary: str, **payload: object) -> AgentEvent:
    return AgentEvent.model_validate(
        {
            "run_id": run_id,
            "stage": stage,
            "type": EventType.DECISION,
            "summary": summary[:500],
            "payload": payload,
        }
    )


def scene_duration_bounds(settings: Settings) -> tuple[float, float]:
    """Per-scene duration range the *real* video model can produce, so plans made in mock mode stay
    producible on fal. Clips are trimmed to the scene duration at assembly."""
    from adsmith.domain.scene import MAX_SCENE_S, MIN_SCENE_S  # noqa: PLC0415

    video = settings.catalog.fal.video
    endpoint = video.endpoints[settings.fal_video_model or video.default]
    model_min = endpoint.min_duration_s or min(endpoint.allowed_durations_s or [MIN_SCENE_S])
    return max(MIN_SCENE_S, model_min), MAX_SCENE_S


def scene_block(scene: Scene) -> str:
    sh = scene.shot
    return "\n".join(
        [
            f"Scene {scene.order} ({scene.purpose.value}, {scene.duration_s:g}s): "
            f"{scene.description}",
            f"  Shot: {sh.shot_type.value}, camera {sh.camera_movement.value}, lens {sh.lens}, "
            f"speed {sh.speed.value}",
            f"  Action: {sh.subject_action}",
            f"  Setting: {sh.setting}; time: {sh.time_of_day.value}",
            f"  Lighting: {sh.lighting}",
            f"  Composition: {sh.composition}",
            f"  Mood: {sh.mood}" + (f"; color: {sh.color_notes}" if sh.color_notes else ""),
            f"  Shows character: {scene.shows_character}; shows product: {scene.shows_product}",
        ]
    )


def storyboard_block(sb: Storyboard) -> str:
    return "\n\n".join(scene_block(s) for s in sb.scenes)


def flag_override_events(
    run_id: str, stage: Stage, overrides: list[FlagOverride]
) -> tuple[dict[str, list[Flag]], list[AgentEvent]]:
    flags: dict[str, list[Flag]] = {}
    for o in overrides:
        flags.setdefault(o.scene_id, []).append(o.flag)
    return flags, [o.as_event(run_id, stage) for o in overrides]
