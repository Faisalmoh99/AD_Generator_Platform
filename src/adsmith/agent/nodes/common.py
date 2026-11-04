"""Shared rendering of domain objects into prompt text (one canonical form per object)."""

from __future__ import annotations

from adsmith.domain.brief import Brief
from adsmith.domain.concept import Concept
from adsmith.domain.enums import Language, Stage
from adsmith.domain.events import AgentEvent, EventType


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
