"""M7: natural-language edits end to end (scripted LLM, real mock media).

The three plan examples, with the provider-call count of each edit as proof that untouched scenes
cost $0, plus clarification and the deterministic pieces (refs, rebalance, bible patch)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from adsmith.agent.edit.blast import apply_bible_changes, rebalance_durations
from adsmith.agent.edit.refs import UnresolvedRefError, resolve_ref
from adsmith.agent.edit.service import EditService
from adsmith.domain.edit import SceneRef
from adsmith.domain.enums import SceneStatus, Speed, Stage, TimeOfDay
from adsmith.domain.events import EventType
from adsmith.domain.run import RunState
from tests.media_helpers import REQUEST, Harness, fast_settings, make_harness

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


async def finished(tmp_path: Path) -> tuple[Harness, RunState]:
    h = make_harness(fast_settings(tmp_path), scenario="perfume_ar")
    run = h.orch.create_run(REQUEST, interactive=False)
    state = await h.orch.run_until_blocked(run.id)
    assert state.stage is Stage.DONE
    return h, state


def snapshot(state: RunState) -> dict[int, tuple[int, str | None, str | None]]:
    assert state.storyboard is not None
    return {
        s.order: (
            s.version,
            s.keyframe.sha256 if s.keyframe else None,
            s.clip.sha256 if s.clip else None,
        )
        for s in state.storyboard.scenes
    }


async def apply(h: Harness, run_id: str, text: str) -> tuple[RunState, int, int]:
    img, vid = h.image.calls, h.video.calls
    outcome = await EditService(h.rt).edit(run_id, text)
    assert outcome.status == "applied", outcome.message
    state = await h.orch.run_until_blocked(run_id)
    assert state.stage is Stage.DONE and state.final is not None
    return state, h.image.calls - img, h.video.calls - vid


def blast_event(h: Harness, run_id: str) -> str:
    evs = [
        e
        for e in h.rt.storage.events.tail(run_id, limit=10_000)
        if e.type is EventType.BLAST_RADIUS
    ]
    return evs[-1].summary


async def test_slower_scene_2_is_clip_depth_only(tmp_path: Path) -> None:
    h, before = await finished(tmp_path)
    old = snapshot(before)
    after, images, videos = await apply(h, before.id, "خل المشهد الثاني أبطأ")
    new = snapshot(after)
    assert (images, videos) == (0, 1)  # one new clip, keyframe kept, nothing else touched
    assert new[2][0] == old[2][0] + 1 and new[2][1] == old[2][1] and new[2][2] != old[2][2]
    assert all(new[o] == old[o] for o in (1, 3, 4))
    assert after.storyboard is not None
    s2 = after.storyboard.scenes[1]
    assert s2.prompt is not None and s2.prompt.template_version.startswith("prompt_rewrite@")
    assert "Blast radius: {'2': 'clip'}" in blast_event(h, before.id)
    assert "untouched (0 provider calls): [1, 3, 4]" in blast_event(h, before.id)


async def test_night_scene_3_propagates_to_scene_4(tmp_path: Path) -> None:
    h, before = await finished(tmp_path)
    old = snapshot(before)
    after, images, videos = await apply(h, before.id, "make scene 3 at night")
    assert (images, videos) == (2, 2)
    new = snapshot(after)
    assert all(new[o] == old[o] for o in (1, 2))
    assert after.storyboard is not None
    s2, s3, s4 = after.storyboard.scenes[1:]
    assert s3.shot.time_of_day is TimeOfDay.NIGHT and s4.shot.time_of_day is TimeOfDay.NIGHT
    assert s2.shot.time_of_day is TimeOfDay.GOLDEN_HOUR  # separate moment: not propagated
    summary = blast_event(h, before.id)
    assert "'3': 'prompt'" in summary and "'4': 'prompt'" in summary
    assert "untouched (0 provider calls): [1, 2]" in summary


async def test_black_thobe_bumps_bible_and_skips_product_only_scene(tmp_path: Path) -> None:
    h, before = await finished(tmp_path)
    assert before.storyboard is not None and before.bible is not None
    showing = [s.order for s in before.storyboard.scenes if s.shows_character]
    old = snapshot(before)
    after, images, videos = await apply(h, before.id, "غيّر لبسه لثوب أسود")
    assert after.bible is not None and after.bible.version == before.bible.version + 1
    assert after.bible.character is not None
    assert after.bible.character.wardrobe[0].startswith("Crisp black")
    assert (images, videos) == (len(showing), len(showing))
    new = snapshot(after)
    for o in (1, 2, 3, 4):
        assert (new[o] != old[o]) is (o in showing)
    assert 4 not in showing  # the product-only hero insert is untouched
    assert "bible v2" in blast_event(h, before.id)


async def test_ambiguous_edit_asks_instead_of_guessing(tmp_path: Path) -> None:
    h, before = await finished(tmp_path)
    outcome = await EditService(h.rt).edit(before.id, "غيّر شي")
    assert outcome.status == "clarify" and "أي مشهد" in outcome.message
    after = h.rt.storage.runs.load(before.id)
    assert after.stage is Stage.DONE and snapshot(after) == snapshot(before) and not after.patches


async def test_edit_is_refused_while_generating(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path), scenario="perfume_ar")
    run = h.orch.create_run(REQUEST, interactive=False)
    from adsmith.agent.edit.service import EditRejectedError  # noqa: PLC0415

    with pytest.raises(EditRejectedError):
        await EditService(h.rt).edit(run.id, "make scene 3 at night")


# ----------------------------------------------------------------------------- pure pieces


async def test_refs_resolve_arabic_and_english_ordinals(tmp_path: Path) -> None:
    _, state = await finished(tmp_path)
    sb = state.storyboard
    assert sb is not None
    ids = [s.id for s in sb.scenes]
    for value, n in [("الثاني", 2), ("المشهد الثالث", 3), ("٢", 2), ("scene 3", 3), ("last", 4),
                     ("الأخير", 4), ("first", 1), ("الأولى", 1)]:  # fmt: skip
        assert resolve_ref(SceneRef(kind="ordinal", value=value), sb) == [ids[n - 1]], value
    assert resolve_ref(SceneRef(kind="all", value=""), sb) == ids
    for bad in ("scene 9", "the nice one", "2 and 3"):
        with pytest.raises(UnresolvedRefError):
            resolve_ref(SceneRef(kind="ordinal", value=bad), sb)


async def test_rebalance_keeps_total_and_bounds(tmp_path: Path) -> None:
    _, state = await finished(tmp_path)
    sb = state.storyboard
    assert sb is not None
    target = sb.scenes[1]
    out = rebalance_durations(sb, target.id, 5.0, min_s=3.0, max_s=8.0)
    durations = {s.id: out.get(s.id, s.duration_s) for s in sb.scenes if s.id != target.id}
    assert sum(durations.values()) + 5.0 == pytest.approx(sb.target_duration_s, abs=0.01)
    assert all(3.0 <= d <= 8.0 for d in durations.values())
    with pytest.raises(ValueError, match="each needs"):
        rebalance_durations(sb, target.id, 8.0, min_s=3.0, max_s=8.0)


async def test_bible_patch_validates_paths_and_lists(tmp_path: Path) -> None:
    _, state = await finished(tmp_path)
    bible = state.bible
    assert bible is not None
    new = apply_bible_changes(bible, {"character.wardrobe": "black thobe; white ghutra"})
    assert new.character is not None and new.character.wardrobe == ["black thobe", "white ghutra"]
    assert new.version == bible.version + 1
    for bad in ("character.shoe_size", "weather.sky", "character"):
        with pytest.raises(ValueError):
            apply_bible_changes(bible, {bad: "x"})


def test_slow_edit_uses_motion_change_not_a_speed_noop() -> None:
    assert Speed.SLOW_MOTION.value == "slow_motion"  # S2 is already slow: motion_change carries it
    assert SceneStatus.STALE.value == "stale"


async def test_the_second_one_is_scene_two(tmp_path: Path) -> None:
    _, state = await finished(tmp_path)
    assert state.storyboard is not None
    got = resolve_ref(SceneRef(kind="ordinal", value="the second one"), state.storyboard)
    assert got == [state.storyboard.scenes[1].id]


async def test_wardrobe_edit_archives_the_bible_and_keeps_the_product_reference(
    tmp_path: Path,
) -> None:
    h = make_harness(fast_settings(tmp_path), scenario="perfume_ar")
    run = h.orch.create_run(REQUEST, interactive=False, product_reference=True)
    before = await h.orch.run_until_blocked(run.id)
    assert before.product_reference is not None and before.product_reference.approved
    assert before.bible is not None
    refs_before = h.reference.calls if h.reference else 0
    after, _images, _videos = await apply(h, before.id, "غيّر لبسه لثوب أسود")
    assert after.bible is not None and after.bible.version == before.bible.version + 1
    assert [b.version for b in after.bible_history][-1] == before.bible.version  # archived
    pr = after.product_reference
    assert pr is not None and pr.valid_for(after)  # the product did not change
    assert pr.tries == before.product_reference.tries  # no new packshot was paid for
    assert h.reference is not None and h.reference.calls >= refs_before
