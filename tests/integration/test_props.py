"""ADR-0026 phase 3: one reference image per prop the whole-ad prompt names (zero keys).

The scripted writer names two props (a brass tray, a coffee cup). They are generated after the
director, checked against the product photo, appended after the user's reference images, and the
References line numbers them; a prop that resembles the product is retried once, then dropped."""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from adsmith.agent.orchestrator import Orchestrator
from adsmith.domain.ad import render_ad_prompt
from adsmith.domain.enums import Stage
from adsmith.domain.events import EventType
from adsmith.domain.run import RunState
from adsmith.providers.llm.scripted import ScriptedLLMProvider
from adsmith.providers.media.mock import MockImageProvider
from tests.integration.test_multishot_pipeline import REQUEST, photo, runtime, settings

PASS = {
    "score": 0.88,
    "checks": {
        "product_visible": True, "matches_shot_spec": True, "lighting_matches": True,
        "continuity_ok": True, "artifacts_free": True, "brand_safe": True, "text_free": True,
        "alcohol_free": True, "key_action_happens": True,
    },
    "issues": [],
    "revised_prompt_hint": "",
    "frames_inspected": 2,
}  # fmt: skip
LOOKS_LIKE_THE_PRODUCT = {
    **PASS,
    "score": 0.4,
    "checks": {**PASS["checks"], "continuity_ok": False},  # type: ignore[dict-item]
    "issues": [
        {
            "category": "product",
            "severity": "major",
            "observation": "The tray is drawn as an amber faceted bottle with a gold dome.",
            "fix": "A flat round hammered brass tray, no glass, no cap.",
            "frame_index": 1,
        }
    ],
    "revised_prompt_hint": "A flat round tray, no glass and no cap.",
}


async def to_estimate(tmp_path: Path, **over: Any) -> tuple[Orchestrator, RunState]:
    steps = over.pop("prop_steps", None)
    refs = over.pop("ref_images", [])
    rt = runtime(settings(tmp_path, **over))
    if steps is not None:
        llm = rt._provider_override
        assert isinstance(llm, ScriptedLLMProvider)
        llm.override("qa_prop_ref", steps)
    orch = Orchestrator(rt)
    run = orch.create_run(
        REQUEST, interactive=False, product_photo=photo(tmp_path), ref_images=refs
    )
    state = await orch.run_until_blocked(run.id, stop_before=Stage.ESTIMATE)
    assert state.stage is Stage.ESTIMATE
    return orch, state


def image_calls(orch: Orchestrator, state: RunState) -> int:
    img = orch.rt.media_gateway(state).image
    assert isinstance(img, MockImageProvider)
    return img.calls


def notes(orch: Orchestrator, run_id: str) -> list[str]:
    evs = orch.rt.storage.events.tail(run_id, limit=3000)
    return [e.summary for e in evs if e.stage == "props" and e.type is EventType.DECISION]


async def test_each_named_prop_gets_an_image_and_a_number(tmp_path: Path) -> None:
    orch, state = await to_estimate(tmp_path)
    refs = state.reference_images
    assert [(r.name, r.source) for r in refs] == [
        ("brass tray", "generated"),
        ("coffee cup", "generated"),
    ]
    assert all(r.cost_usd == pytest.approx(0.025) and r.check.startswith("passed") for r in refs)
    assert state.budget.by_category["image"] == pytest.approx(0.05)
    assert image_calls(orch, state) == 2
    g = state.generation
    assert g is not None
    assert "Image 2 is the brass tray: a round" in g.prompt.text
    assert (
        "Image 3 is the coffee cup: a small handleless Arabic coffee cup (finjan)" in g.prompt.text
    )
    assert notes(orch, state.id)[-1].startswith(
        "Props: references renumbered: Image 1: the product photo; Image 2: brass tray"
    )
    pack = orch.rt.storage.artifacts.run_dir(state.id) / "production_pack" / "storyboard.md"
    assert "- Image 2: brass tray (generated, $0.025" in pack.read_text()


async def test_a_user_image_with_the_same_name_wins(tmp_path: Path) -> None:
    tray = tmp_path / "Brass_Tray.png"
    Image.new("RGB", (64, 64), (180, 140, 60)).save(tray)
    orch, state = await to_estimate(tmp_path, ref_images=[tray])
    assert [(r.name, r.source) for r in state.reference_images] == [
        ("Brass_Tray.png", "user"),
        ("coffee cup", "generated"),
    ]
    assert image_calls(orch, state) == 1
    assert state.generation is not None
    assert "Image 2 is the brass tray" in state.generation.prompt.text


async def test_a_prop_that_looks_like_the_product_is_retried_then_dropped(
    tmp_path: Path,
) -> None:
    steps = [{"when": "Name: brass tray", "output": LOOKS_LIKE_THE_PRODUCT}, {"output": PASS}]
    orch, state = await to_estimate(tmp_path, qa_mode="llm", prop_steps=steps)
    assert [r.name for r in state.reference_images] == ["coffee cup"]
    assert image_calls(orch, state) == 3  # the tray twice, the cup once
    assert state.budget.by_category["image"] == pytest.approx(0.075)  # rejected images are paid
    g = state.generation
    assert g is not None
    # A dropped prop has no image, so no References line: it stays in the shot text only.
    assert "brass tray" not in g.prompt.text.splitlines()[1]
    assert "Image 2 is the coffee cup" in g.prompt.text
    assert "brass tray" in g.prompt.shots[0].action  # the shot still names the prop
    text = "\n".join(notes(orch, state.id))
    assert "the brass tray image was rejected" in text and "it is dropped" in text
    second = [r for r in orch.rt._provider_override.requests  # type: ignore[union-attr]
              if r.template == "qa_prop_ref@v2"][1]  # fmt: skip
    assert second.messages[0].parts[-1].text.count("A flat round tray, no glass and no cap") == 1


async def test_the_props_cap_refuses_before_the_call(tmp_path: Path) -> None:
    orch, state = await to_estimate(tmp_path, props_max_usd=0.03)
    assert [r.name for r in state.reference_images] == ["brass tray"]
    assert image_calls(orch, state) == 1
    assert any("above the $0.03 props cap" in n for n in notes(orch, state.id))


async def test_a_cached_prop_costs_nothing(tmp_path: Path) -> None:
    orch, first = await to_estimate(tmp_path)
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    second = await orch.run_until_blocked(run.id, stop_before=Stage.ESTIMATE)
    assert [r.cost_usd for r in second.reference_images] == [0.0, 0.0]
    assert [r.artifact for r in second.reference_images] == [
        r.artifact for r in first.reference_images
    ]
    assert any(", cached)" in n for n in notes(orch, second.id))


async def test_props_are_dropped_last_first_when_the_prompt_is_too_long(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import adsmith.agent.props as props_module  # noqa: PLC0415

    rt = runtime(settings(tmp_path))
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    state = await orch.run_until_blocked(run.id, stop_before=Stage.PROPS)
    assert state.generation is not None
    g0 = state.generation
    # Room for the first prop's References line only.
    limit = len(
        render_ad_prompt(
            g0.prompt, duration_s=g0.prompt.duration_s, aspect_ratio=g0.aspect_ratio,
            image_names=["brass tray"],
        )
    )  # fmt: skip

    def at_limit(s: Any, model: str | None) -> Any:
        return SimpleNamespace(max_prompt_chars=limit)

    monkeypatch.setattr(props_module, "ad_endpoint", at_limit)
    state = await orch.run_until_blocked(run.id, stop_before=Stage.ESTIMATE)
    assert [r.name for r in state.reference_images] == ["brass tray"]
    assert state.generation is not None and len(state.generation.prompt.text) <= limit
    assert any("the coffee cup image is dropped" in n for n in notes(orch, state.id))


async def test_a_prop_that_does_not_earn_an_image_stays_text_only(tmp_path: Path) -> None:
    rt = runtime(settings(tmp_path))
    llm = rt._provider_override
    assert isinstance(llm, ScriptedLLMProvider)
    writer = copy.deepcopy(llm._scripts["ad_prompt_writer"][0])
    tray = writer["output"]["references"][0]
    tray.update({"shots": [1], "handled": False, "max_size": "small", "lookalike": False})
    llm.override("ad_prompt_writer", [writer])
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    state = await orch.run_until_blocked(run.id, stop_before=Stage.ESTIMATE)
    assert [r.name for r in state.reference_images] == ["coffee cup"]
    assert image_calls(orch, state) == 1
    assert state.generation is not None
    assert "brass tray" not in state.generation.prompt.text.splitlines()[1]
    assert any("brass tray stay in the shot text only" in n for n in notes(orch, state.id))


async def test_a_generic_version_of_a_gulf_object_fails_the_check(tmp_path: Path) -> None:
    generic = {
        **PASS,
        "score": 0.55,
        "checks": {**PASS["checks"], "matches_shot_spec": False},  # type: ignore[dict-item]
        "issues": [
            {
                "category": "composition",
                "severity": "major",
                "observation": "A white mug with a handle, not a handleless finjan.",
                "fix": "A small handleless white porcelain finjan with a thin gold band.",
                "frame_index": 1,
            }
        ],
    }
    steps = [{"when": "Name: coffee cup", "output": generic}, {"output": PASS}]
    orch, state = await to_estimate(tmp_path, qa_mode="llm", prop_steps=steps)
    assert [r.name for r in state.reference_images] == ["brass tray"]  # the cup is dropped
    req = next(
        r
        for r in orch.rt._provider_override.requests  # type: ignore[union-attr]
        if r.template == "qa_prop_ref@v2" and "Name: coffee cup" in r.messages[0].parts[-1].text
    )
    body = req.messages[0].parts[-1].text
    assert "the traditional version is required): yes" in body
    assert "Never: a mug or any cup with a handle" in body
    # The image prompt itself asks for the Gulf version and names the wrong ones.
    g = state.generation
    assert g is not None
    cup = next(r for r in g.prompt.references if r.name == "coffee cup")
    image_prompt = orch.rt.props_runner(state)._prompt(state, cup, "")
    assert "a small handleless Arabic coffee cup (finjan)" in image_prompt
    assert "The traditional Gulf version, never a mug or any cup with a handle" in image_prompt


def _library_runtime(tmp_path: Path, photo_colour: tuple[int, int, int], **over: Any) -> Any:
    """A runtime whose culture bank points the finjan at a temp 'verified' photo."""
    s = settings(tmp_path, props_library=True, **over)
    cup = tmp_path / "finjan.png"
    Image.new("RGB", (256, 256), photo_colour).save(cup)
    bank = s.culture_bank
    objects = [
        o.model_copy(update={"ref_image": str(cup)}) if o.id == "finjan" else o
        for o in bank.objects
    ]
    s.__dict__["culture_bank"] = bank.model_copy(update={"objects": objects})
    return runtime(s)


async def test_a_library_photo_replaces_the_generated_prop(tmp_path: Path) -> None:
    rt = _library_runtime(tmp_path, (240, 240, 235), qa_mode="llm")
    llm = rt._provider_override
    assert isinstance(llm, ScriptedLLMProvider)
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    state = await orch.run_until_blocked(run.id, stop_before=Stage.ESTIMATE)
    refs = {r.name: r for r in state.reference_images}
    assert refs["coffee cup"].source == "library" and refs["coffee cup"].cost_usd == 0.0
    assert refs["brass tray"].source == "generated"
    assert image_calls(orch, state) == 1  # only the tray is generated
    assert any(
        n.startswith("Props: prop from library: the coffee cup") for n in notes(orch, state.id)
    )
    checks = llm.calls_for("qa_prop_ref")
    # A second run reuses the cached check: no new call for the library photo.
    run2 = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    state2 = await orch.run_until_blocked(run2.id, stop_before=Stage.ESTIMATE)
    assert {r.name: r.check for r in state2.reference_images}["coffee cup"] == (
        "library, cached pass"
    )
    # Only the generated tray is checked again; the library photo's check is cached.
    assert llm.calls_for("qa_prop_ref") == checks + 1


async def test_a_rejected_library_photo_falls_back_to_generation(tmp_path: Path) -> None:
    rt = _library_runtime(tmp_path, (90, 60, 40), qa_mode="llm")
    llm = rt._provider_override
    assert isinstance(llm, ScriptedLLMProvider)
    bad = {**LOOKS_LIKE_THE_PRODUCT, "revised_prompt_hint": ""}
    llm.override("qa_prop_ref", [{"when": "library photo", "output": bad}, {"output": PASS}])
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    state = await orch.run_until_blocked(run.id, stop_before=Stage.ESTIMATE)
    assert {r.name: r.source for r in state.reference_images}["coffee cup"] == "generated"
    assert any("library photo of the coffee cup was rejected" in n for n in notes(orch, state.id))
