"""PLAN_V2: a scripted montage run end to end on mock media, plus a one-shot edit."""

from __future__ import annotations

from pathlib import Path

from adsmith.agent import commands
from adsmith.domain.enums import SceneStatus, Stage
from adsmith.domain.formats import AdFormat
from tests.media_helpers import fast_settings, make_harness

REQUEST = "Make me a 12-second ad for a luxury men's perfume for young men, luxurious and bold."


async def test_montage_run_end_to_end_on_mock_media(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path), scenario="montage_en")
    run = h.orch.create_run(REQUEST, interactive=False)
    state = await h.orch.run_until_blocked(run.id)
    assert state.stage is Stage.DONE, state.error
    assert state.brief is not None and state.brief.format is AdFormat.MONTAGE
    assert state.storyboard is not None and state.storyboard.format is AdFormat.MONTAGE
    scenes = state.storyboard.scenes
    assert len(scenes) == 9 and all(s.status is SceneStatus.APPROVED for s in scenes)
    assert state.final is not None and state.final.duration_s is not None
    assert abs(state.final.duration_s - (12.0 + state.config.endcard_s)) <= 0.2
    assert h.llm is not None
    for template in ("ideate@", "storyboard@"):
        req = next(r for r in h.llm.requests if r.template.startswith(template))
        text = "\n".join(p.text for m in req.messages for p in getattr(m, "parts", ()))
        assert "<ad_format>" in text and "Format: montage" in text, template


async def test_editing_one_montage_shot_regenerates_only_that_shot(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path), scenario="montage_en")
    run = h.orch.create_run(REQUEST, interactive=False)
    done = await h.orch.run_until_blocked(run.id)

    def all_calls() -> int:
        extra = sum(p.calls for p in (h.reference, h.composite) if p is not None)
        return h.provider_calls + extra

    calls_before = all_calls()
    commands.edit_scene_spec(
        h.rt.storage, done.id, 3, reason="tighter", shot={"lens": "100mm macro f/5.6"}
    )
    after = await h.orch.run_until_blocked(done.id)
    assert after.stage is Stage.DONE
    assert after.storyboard is not None
    versions = [s.version for s in after.storyboard.scenes]
    assert versions == [1, 1, 2, 1, 1, 1, 1, 1, 1]
    # A crop keyframe depends only on the packshot, the region and the entry's lighting: the lens
    # edit reuses it from the cache, and only the clip is generated again.
    assert all_calls() - calls_before == 1


async def test_identity_mode_is_routed_per_shot_from_the_vocabulary(tmp_path: Path) -> None:
    """PLAN_V2 item 3: montage turns the locked reference on by itself; each keyframe uses the
    mode its vocabulary entry declares (5 direct, 2 crop, 1 composite, 1 plain in the fixture)."""
    h = make_harness(fast_settings(tmp_path), scenario="montage_en")
    run = h.orch.create_run(REQUEST, interactive=False)
    assert run.config.product_reference is False  # not asked for: the format implies it
    state = await h.orch.run_until_blocked(run.id)
    assert state.product_reference is not None and state.product_reference.approved
    assert h.reference is not None and h.composite is not None
    assert (h.reference.calls, h.composite.calls) == (5 + 2, 1)  # direct + crop relights
    assert h.image.calls == 1 + 1 + 1  # packshot + 1 product-free shot + 1 composite base
    events = [
        e
        for e in h.rt.storage.events.tail(run.id, limit=10_000)
        if e.type.value == "attempt_started" and "identity" in e.payload
    ]
    modes = {str(e.payload["vocab"]): e.payload["identity"] for e in events}
    assert modes["cap_macro"] == "crop" and modes["glass_edge_macro"] == "crop"
    assert modes["hand_cuff"] == "composite"
    assert modes["spray_from_nozzle"] == "direct" and modes["water_drop_leaf"] == "none"
    assert state.storyboard is not None
    cap = state.storyboard.scenes[0]
    kf = next(a for a in cap.attempts if a.phase.value == "keyframe")
    assert "Do not zoom, re-frame" in kf.prompt_used  # macro: cut from the packshot, relit
    assert kf.model.startswith("crop:cap+")
    spray = state.storyboard.scenes[4]
    kf = next(a for a in spray.attempts if a.phase.value == "keyframe")
    assert "the cap is off" in kf.prompt_used


async def test_switching_a_shot_to_another_vocabulary_entry(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path), scenario="montage_en")
    run = h.orch.create_run(REQUEST, interactive=False)
    done = await h.orch.run_until_blocked(run.id)
    entry = h.rt.settings.vocabulary.shots["light_sweep"]
    commands.edit_scene_spec(
        h.rt.storage,
        done.id,
        2,
        reason="drop shot replaced",
        description="A band of light crosses the bottle.",
        shot={"subject_action": "A band of hard light travels slowly across the bottle."},
        vocab=("light_sweep", entry.shows_person, entry.needs_product),
    )
    after = await h.orch.run_until_blocked(done.id)
    assert after.stage is Stage.DONE and after.storyboard is not None
    s2 = after.storyboard.scenes[1]
    assert (s2.vocab, s2.shows_product, s2.version) == ("light_sweep", True, 2)
    assert s2.history[-1].vocab == "water_drop_leaf"
