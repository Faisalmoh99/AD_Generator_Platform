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
    calls_before = h.provider_calls
    commands.edit_scene_spec(
        h.rt.storage, done.id, 3, reason="tighter", shot={"lens": "100mm macro f/5.6"}
    )
    after = await h.orch.run_until_blocked(done.id)
    assert after.stage is Stage.DONE
    assert after.storyboard is not None
    versions = [s.version for s in after.storyboard.scenes]
    assert versions == [1, 1, 2, 1, 1, 1, 1, 1, 1]
    assert h.provider_calls - calls_before == 2  # one keyframe + one clip
