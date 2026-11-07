"""Shared rendering of domain objects into prompt text (one canonical form per object)."""

from __future__ import annotations

from adsmith.config import AdVideoEndpoint, FalVideoEndpoint, Settings
from adsmith.domain.brief import Brief
from adsmith.domain.concept import Concept, InsightSet
from adsmith.domain.culture_bank import CultureBank
from adsmith.domain.enums import Language, Stage
from adsmith.domain.events import AgentEvent, EventType
from adsmith.domain.formats import AdFormat
from adsmith.domain.run import RunState
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
        *([f"Requested shots: exactly {b.requested_shots}"] if b.requested_shots else []),
        f"Ad format: {b.format.value}",
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


def concept_block(c: Concept, number: int | None = None, *, image_first: bool = False) -> str:
    """``image_first`` (the judge, ADR-0029): the device's one image right after the title, before
    the prose, so a compact spec is read before the richest writing."""
    head = f"Concept {number}: {c.title}" if number else f"Concept: {c.title}"
    lines = [head]
    if image_first and c.device_id:
        lines.append(f"The image ({c.device_id}): {c.device_image or '-'}")
    lines += [f"Logline: {c.logline}", f"Insight: {c.insight}"]
    if c.story is not None:
        lines.append(
            f"Story: setup: {c.story.setup} | turn: {c.story.turn} | payoff: {c.story.payoff}"
        )
    else:
        lines.append(f"Beats: {' → '.join(c.narrative_arc)}")
    if c.hook is not None:
        lines.append(
            f"Hook (0-2 s): {c.hook.image} | sound: {c.hook.sound} | opens: {c.hook.question}"
        )
    else:
        lines.append(f"Visual hook: {c.visual_hook}")
    if c.device_id and not image_first:
        at = f", shot {c.device_shot}" if c.device_shot else ""
        lines.append(f"Visual device ({c.device_id}{at}): {c.device_image or '-'}")
    if c.product_role is not None:
        lines.append(f"Product role: {c.product_role.role}: {c.product_role.line}")
    if c.cultural_meaning:
        lines.append(f"Cultural meaning: {c.cultural_meaning}")
    if c.originality_claim:
        lines.append(f"Why it is ownable: {c.originality_claim}")
    if c.shot_sketch:
        lines.append("Shots: " + " / ".join(c.shot_sketch))
    lines += [
        f"Emotional driver: {c.emotional_driver}",
        f"Style: {', '.join(c.style_references) or '-'}",
    ]
    return "\n".join(lines)


def insights_block(insights: InsightSet | None, bank: CultureBank | None = None) -> str:
    if insights is None or not insights.insights:
        return "(none were given: find the human truth yourself and state it in insight)"
    lines = []
    for i, x in enumerate(insights.insights, 1):
        line = (
            f"{i}. {x.statement}\n   tension: {x.tension}\n   evidence: {x.evidence}\n"
            f"   for a Gulf viewer: {x.gulf_meaning}"
        )
        entry = bank.get(x.culture_anchor) if bank is not None else None
        if entry is not None:
            line += f"\n   culture anchor: «{entry.text_ar}»: {entry.meaning_en}"
            if entry.pitfalls:
                line += f" Never: {'; '.join(entry.pitfalls)}."
        lines.append(line)
    return "\n".join(lines)


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


def _video_endpoint(settings: Settings, video_model: str | None = None) -> FalVideoEndpoint:
    video = settings.catalog.fal.video
    return video.endpoints[video_model or settings.fal_video_model or video.default]


def scene_duration_bounds(
    settings: Settings, video_model: str | None = None, fmt: AdFormat = AdFormat.NARRATIVE
) -> tuple[float, float, float]:
    """(per-scene minimum, per-scene maximum in a multi-scene ad, maximum for a single-take ad).

    Narrative: what the *real* video model can produce, so plans made in mock mode stay
    producible on fal (clips are trimmed to the scene duration at assembly). Montage: the kept
    window of each shot (formats.toml); the model still renders its own minimum, and assembly keeps
    only the window (``render_window``)."""
    from adsmith.domain.scene import MAX_SCENE_S, MULTI_SCENE_MAX_S  # noqa: PLC0415

    endpoint = _video_endpoint(settings, video_model)
    model_max = endpoint.max_duration_s or max(endpoint.allowed_durations_s or [MAX_SCENE_S])
    single = endpoint.profile.max_single_shot_s if endpoint.profile else model_max
    single_max = min(single, model_max, MAX_SCENE_S)
    if fmt in {AdFormat.MONTAGE, AdFormat.MULTISHOT}:
        g = settings.formats.grammar(fmt)
        return g.kept_min, g.kept_max, g.kept_max
    narrative = settings.formats.grammar(AdFormat.NARRATIVE)
    model_min = endpoint.min_duration_s or min(endpoint.allowed_durations_s or [narrative.kept_min])
    return max(narrative.kept_min, model_min), MULTI_SCENE_MAX_S, single_max


def _ad_endpoint(settings: Settings, ad_model: str | None) -> AdVideoEndpoint:
    from adsmith.agent.context import ad_endpoint  # noqa: PLC0415

    return ad_endpoint(settings, ad_model)


def profile_block_for(settings: Settings, state: RunState) -> str:
    """The capability profile of the model that will render this run: the reference-to-video
    model for a multishot run, else the per-scene video model."""
    if state.brief is not None and state.brief.format is AdFormat.MULTISHOT:
        return video_profile_block(settings, ad_model=state.config.ad_model, whole_ad=True)
    return video_profile_block(settings, state.config.video_model)


def video_profile_block(
    settings: Settings,
    video_model: str | None = None,
    *,
    ad_model: str | None = None,
    whole_ad: bool = False,
) -> str:
    """The target video model's capability profile as prompt text (ideate, select, storyboard)."""
    endpoint: FalVideoEndpoint | AdVideoEndpoint = (
        _ad_endpoint(settings, ad_model) if whole_ad else _video_endpoint(settings, video_model)
    )
    p = endpoint.profile
    if p is None:
        return "(no capability profile for this video model)"
    lines = [
        f"Video model: {endpoint.endpoint} ("
        + (
            f"one generation renders the whole ad, up to {p.max_single_shot_s:g} s, in "
            "several shots with native sound"
            if whole_ad
            else f"one continuous take up to {p.max_single_shot_s:g} s"
        )
        + ")"
    ]
    lines += ["Renders well:", *(f"- {s}" for s in p.strengths)]
    lines += ["Renders badly:", *(f"- {w}" for w in p.weaknesses)]
    return "\n".join(lines)


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
            *([f"  Vocab: {scene.vocab}"] if scene.vocab else []),
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
