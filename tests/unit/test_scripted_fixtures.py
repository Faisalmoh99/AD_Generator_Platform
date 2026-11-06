"""Packaged scripted fixtures can never drift from the domain model.

1. Every fixture file belongs to a template with a registered output schema.
2. Every ``output`` step validates against that schema.
3. Each scenario is coherent end-to-end through the real domain conversions: brief → concepts →
   storyboard (durations sum to the brief's target) → bible, and scene flags need no override.
4. Arabic strings are stored in logical order: no presentation forms (what visually shaped,
   reversed copy-paste produces) and no bidi control characters.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from adsmith.agent.schemas import OUTPUT_SCHEMAS
from adsmith.domain.brief import Brief, BriefDraft
from adsmith.domain.concept import ConceptSet, CreativeConceptSetDraft, InsightSetDraft
from adsmith.domain.continuity import ContinuityBible, ContinuityBibleDraft
from adsmith.domain.scene_flags import enforce_scene_flags
from adsmith.domain.shot_lint import multi_action_problem, negative_contradictions
from adsmith.domain.storyboard import Storyboard, StoryboardDraft
from adsmith.providers.llm.scripted import FIXTURES_DIR

SCENARIOS = sorted(p for p in FIXTURES_DIR.iterdir() if p.is_dir())
FIXTURE_FILES = sorted(FIXTURES_DIR.glob("*/*.json"))
_PRESENTATION_FORMS = re.compile(r"[\ufb50-\ufdff\ufe70-\ufeff]")
_BIDI_CONTROLS = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def _steps(path: Path) -> list[dict[str, Any]]:
    steps = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(steps, list) and steps, f"{path}: must be a non-empty list of steps"
    return steps


def _outputs(scenario: Path, template: str) -> list[dict[str, Any]]:
    return [s["output"] for s in _steps(scenario / f"{template}.json") if "output" in s]


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def test_there_are_scenarios() -> None:
    assert SCENARIOS, "expected at least one packaged scenario"


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_fixture_outputs_validate_against_registered_schema(path: Path) -> None:
    schema = OUTPUT_SCHEMAS.get(path.stem)
    assert schema is not None, f"{path.stem!r} has fixtures but no registered output schema"
    for i, step in enumerate(_steps(path)):
        if "output" in step:
            schema.model_validate(step["output"]), f"step {i}"


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_arabic_is_stored_in_logical_order(path: Path) -> None:
    for text in _strings(_steps(path)):
        assert not _PRESENTATION_FORMS.search(text), f"presentation forms in {text[:40]!r}"
        assert not _BIDI_CONTROLS.search(text), f"bidi control chars in {text[:40]!r}"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda p: p.name)
def test_scenario_is_coherent_through_domain_conversions(scenario: Path) -> None:
    brief = Brief.from_draft(
        BriefDraft.model_validate(_outputs(scenario, "understand")[0]),
        raw_request="fixture",
        default_aspect="9:16",
    )
    found = InsightSetDraft.model_validate(_outputs(scenario, "insight")[0])
    concepts = ConceptSet.from_creative(
        CreativeConceptSetDraft.model_validate(_outputs(scenario, "ideate")[0]),
        list(found.category_cliches),
    )
    assert all(
        c.insight_number and c.insight_number <= len(found.insights) for c in concepts.concepts
    ), "a concept points at an insight that does not exist"
    storyboard = Storyboard.from_draft(
        StoryboardDraft.model_validate(_outputs(scenario, "storyboard")[0]),
        concept_id=concepts.concepts[0].id,
        target_duration_s=brief.duration_s,
    )
    bible = ContinuityBible(
        **ContinuityBibleDraft.model_validate(_outputs(scenario, "continuity")[0]).model_dump()
    )
    # Fixtures should already be honest about what each scene shows.
    assert enforce_scene_flags(storyboard.scenes, bible) == []
    assert any(s.shows_product for s in storyboard.scenes), "no product shot in the storyboard"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda p: p.name)
def test_scenario_passes_production_lints(scenario: Path) -> None:
    """Fixtures must pass the same lints the nodes enforce (else the scripted run would fail)."""
    scenes = StoryboardDraft.model_validate(_outputs(scenario, "storyboard")[0]).scenes
    for i, s in enumerate(scenes, 1):
        assert multi_action_problem(s.shot.subject_action) is None, f"scene {i}"
    if not (scenario / "prompt_writer.json").is_file():
        return  # a whole-ad scenario (multishot) writes one ad prompt, not per-scene prompts
    prompts = _outputs(scenario, "prompt_writer")
    for i, (s, p) in enumerate(zip(scenes, prompts, strict=False), 1):
        clashes = negative_contradictions(
            s.shot.subject_action, s.shot.composition, p["negative_prompt"]
        )
        assert clashes == [], f"scene {i}: {clashes}"
