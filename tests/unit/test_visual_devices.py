"""ADR-0029 phase 1: the visual-device library, device-first ideate (one device per concept, raw
sketches first, different devices, no copied exemplar, the film's shot count), and recent
winners' devices counted as reuse. Zero keys."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from adsmith.agent.nodes.common import concept_block
from adsmith.agent.nodes.ideate import device_problems
from adsmith.agent.orchestrator import Orchestrator
from adsmith.config import Settings
from adsmith.domain.concept import CreativeConceptSetDraft, MotifEntry, pick_distinct
from adsmith.domain.enums import Stage
from adsmith.providers.llm.scripted import FIXTURES_DIR
from adsmith.storage import Storage
from tests.integration.test_multishot_pipeline import REQUEST, photo, runtime, settings

LIB = Settings().visual_devices


def _draft() -> CreativeConceptSetDraft:
    raw = json.loads((FIXTURES_DIR / "multishot_en" / "ideate.json").read_text())
    return CreativeConceptSetDraft.model_validate(raw[0]["output"])


def test_every_device_fits_a_known_category_and_has_a_proof_when_proven() -> None:
    known = set(Settings().culture_bank.aliases) | {"any"}
    assert len(LIB.devices) >= 10
    for d in LIB.devices:
        assert set(d.categories) <= known, d.id
        assert (d.renderability == "proven") == (d.proven_by is not None), d.id
    assert LIB.get("transformation") is not None
    assert LIB.get("transformation").renderability == "proven"  # type: ignore[union-attr]


def test_every_renderable_wording_passes_the_banned_words_and_the_physics_library() -> None:
    rules = Settings().creative_rules
    for d in LIB.devices:
        for b in rules.banned:
            if b.fields == "all":
                assert not b.hits(d.seedance), (d.id, b.word)
        for r in rules.physics:
            if re.search(r.trigger, d.seedance, re.IGNORECASE):
                assert re.search(r.required, d.seedance, re.IGNORECASE), (d.id, r.id)


def test_a_concept_that_copies_a_worked_example_is_caught() -> None:
    hit = LIB.copied_exemplar(["boy", "father", "garden"])
    assert hit is not None and hit.id == "scent_trail_boy"
    assert LIB.copied_exemplar(["elder", "incense burner"]) is not None
    assert LIB.copied_exemplar(["son", "car", "dawn road"]) is None


def test_devices_are_offered_by_category() -> None:
    coffee = {d.id for d in LIB.for_categories(["coffee"])}
    assert {"time_freeze", "product_pov", "invisible_made_visible"} <= coffee
    assert all(d.fits(["perfume"]) for d in LIB.for_categories(["perfume"]))


def test_the_fixture_passes_the_device_checks() -> None:
    allowed = [d.id for d in LIB.for_categories(["perfume"])]
    assert device_problems(_draft(), allowed, 4, LIB) == []


def test_device_checks_send_back_an_unknown_device_a_repeat_a_copy_and_the_wrong_length() -> None:
    d = _draft()
    allowed = [x.id for x in LIB.for_categories(["perfume"])]
    c = d.concepts
    c[0].device_id = "lens_flare"
    c[2].device_id = c[1].device_id
    c[3].motifs = ["boy", "father", "corridor"]
    c[4].shot_sketch = c[4].shot_sketch[:3]
    c[5].device_shot = 6
    text = "; ".join(device_problems(d, allowed, 4, LIB))
    assert "'lens_flare' is not one of the listed devices" in text
    assert "each concept uses a different device" in text
    assert "copies the worked example 'scent_trail_boy'" in text
    assert "concept 5: the film has exactly 4 shots" in text
    assert "concept 6: device_shot 6 is past its 4 shot_sketch lines" in text


def test_a_recent_winners_device_goes_last() -> None:
    d = _draft()
    concepts = [c.to_concept() for c in d.concepts]
    motifs = [c.motifs for c in d.concepts]
    devices = [c.device_id for c in d.concepts]
    fresh = pick_distinct(concepts, motifs, [], devices=devices)
    used = MotifEntry(run_id="r", title="t", axis="self", motifs=["x", "y"], device=devices[1])
    worn = pick_distinct(concepts, motifs, [used], devices=devices)
    assert 1 in fresh and 1 not in worn


def test_the_ledger_keeps_the_device(tmp_path: Path) -> None:
    s = Storage.open(tmp_path)
    s.motifs.record(MotifEntry(run_id="r1", title="t", axis="self", motifs=["a", "b"],
                               device="transformation"))  # fmt: skip
    s.motifs.record(MotifEntry(run_id="r2", title="u", axis="self", motifs=["c", "d"]))
    got = {e.run_id: e.device for e in s.motifs.recent(10)}
    assert got == {"r1": "transformation", "r2": None}


def test_the_concept_block_shows_the_device_to_later_stages() -> None:
    c = _draft().concepts[0].to_concept()
    text = concept_block(c)
    assert f"Visual device (invisible_made_visible, shot {c.device_shot}): " in text


@pytest.mark.parametrize("fixture", ["perfume_en", "perfume_ar", "montage_en", "multishot_en"])
def test_every_scenario_writes_sketches_first(fixture: str) -> None:
    raw = json.loads((FIXTURES_DIR / fixture / "ideate.json").read_text())
    out = raw[0]["output"]
    assert next(iter(out)) == "device_sketches" and len(out["device_sketches"]) >= 6


async def test_ideate_is_shown_the_devices_examples_and_shot_count(tmp_path: Path) -> None:
    rt = runtime(settings(tmp_path))
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    state = await orch.run_until_blocked(run.id, stop_before=Stage.STORYBOARD)
    llm = rt._provider_override
    asked = str(next(r for r in llm.requests if r.template == "ideate@v9").messages)  # type: ignore[union-attr]
    assert "transformation (Transformation)" in asked and "Wording that renders" in asked
    assert "Why it works:" in asked
    assert "every shot_sketch has exactly 4 lines" in asked
    assert state.concepts is not None
    assert len(state.concepts.device_sketches) == 6  # stored for the decisions report
    assert [c.device_id for c in state.concepts.concepts] == [
        "invisible_made_visible",
        "room_responds",
        "rewind",
    ]
