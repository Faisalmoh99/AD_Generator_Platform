"""PLAN_V2 item 8: best-of-2 only for vocabulary key shots, and only when the budget covers it."""

from __future__ import annotations

from pathlib import Path

from adsmith.domain.enums import ScenePhase, Stage
from tests.media_helpers import fast_settings, make_harness

REQUEST = "Make me a 12-second ad for a luxury men's perfume for young men, luxurious and bold."


def clip_takes(state: object) -> dict[str, int]:
    sb = state.storyboard  # type: ignore[attr-defined]
    return {sc.vocab: sum(1 for a in sc.attempts if a.phase is ScenePhase.CLIP) for sc in sb.scenes}


async def test_key_shot_gets_two_takes_others_one(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path), scenario="montage_en")
    run = h.orch.create_run(REQUEST, interactive=False)
    state = await h.orch.run_until_blocked(run.id)
    assert state.stage is Stage.DONE
    takes = clip_takes(state)
    for key in ("hero_push_in", "spray_from_nozzle", "bottle_into_water"):  # hero + events
        assert takes.pop(key) == 2, key
    assert set(takes.values()) == {1}
    summaries = [e.summary for e in h.rt.storage.events.tail(run.id, limit=10_000)]
    assert any("hero_push_in is a key shot: best-of-2" in s for s in summaries)


async def test_tight_budget_falls_back_to_one_take(tmp_path: Path) -> None:
    probe = make_harness(fast_settings(tmp_path / "probe"), scenario="montage_en")
    full = await probe.orch.run_until_blocked(probe.orch.create_run(REQUEST, interactive=False).id)
    # One mock clip = the default video endpoint (Kling v3 Pro, $0.14/s) x its 3 s minimum render.
    one_take = full.budget.spent_usd - 3 * 0.42  # the run with one take on each of 3 key shots
    h = make_harness(
        fast_settings(tmp_path / "tight", budget_usd=round(one_take + 0.2, 2)),
        scenario="montage_en",
    )
    run = h.orch.create_run(REQUEST, interactive=False)
    state = await h.orch.run_until_blocked(run.id)
    assert state.stage is Stage.DONE, state.error
    assert set(clip_takes(state).values()) == {1}
    summaries = [e.summary for e in h.rt.storage.events.tail(run.id, limit=10_000)]
    assert any("hero_push_in is a key shot: 1 take" in s for s in summaries)
