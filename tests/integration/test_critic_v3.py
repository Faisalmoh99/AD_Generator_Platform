"""ADR-0026 phase 5: critic v3. Cuts detected by code drive the shot windows; the first two
seconds and the key shot are sampled densely; execution checks warn (never decide)."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any

import pytest

from adsmith.agent.qa import ad_shot_windows, key_shot_number
from adsmith.domain.ad import HumanPrompt, parse_prompt_shots
from adsmith.domain.events import EventType
from adsmith.domain.qa import AdVerdict
from adsmith.media.frames import cuts_from_profile, detect_cuts
from tests.integration.test_ad_critic import llm_run, verdict_output
from tests.integration.test_human_prompt import HUMAN
from tests.integration.test_multishot_pipeline import REQUEST, photo
from tests.unit.test_ad_generation import generation


def _clip(path: Path, colours: list[tuple[str, float]]) -> Path:
    """Solid colour shots joined by hard cuts (ffmpeg, local)."""
    inputs: list[str] = []
    for colour, seconds in colours:
        inputs += ["-f", "lavfi", "-i", f"color=c={colour}:s=96x160:r=24:d={seconds}"]
    n = len(colours)
    chain = "".join(f"[{i}:v]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]"

    async def make() -> None:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
            "-filter_complex", chain, "-map", "[v]", "-pix_fmt", "yuv420p", str(path),
        )  # fmt: skip
        assert await proc.wait() == 0

    asyncio.run(make())
    return path


def test_hard_cuts_are_found_where_the_shots_change(tmp_path: Path) -> None:
    # Shots of clearly different brightness: the measure is luma, so two shots of the same
    # brightness and different hue (pure red vs pure green) would not read as a cut.
    clip = _clip(tmp_path / "c.mp4", [("black", 1.0), ("white", 1.5), ("gray", 1.0)])
    cuts = asyncio.run(detect_cuts(clip))
    assert len(cuts) == 2
    assert cuts[0] == pytest.approx(1.0, abs=0.05) and cuts[1] == pytest.approx(2.5, abs=0.05)


def test_a_fade_or_steady_motion_is_not_a_cut() -> None:
    ramp = [3.0 + 0.1 * (i % 7) for i in range(120)]  # a camera move
    fade = [*ramp[:40], *(8.0 for _ in range(12)), *ramp[52:]]  # a 0.5 s crossfade
    assert cuts_from_profile(ramp, fps=24) == [] and cuts_from_profile(fade, fps=24) == []
    spiky = [*ramp[:30], 60.0, *ramp[31:]]
    assert cuts_from_profile(spiky, fps=24) == [round(31 / 24, 3)]


def test_observed_cuts_become_the_windows_only_when_they_match_the_plan() -> None:
    g = generation()  # 4 planned shots of 3 s in a 12 s ad
    observed = ad_shot_windows(g, 12.0, [1.2, 6.5, 9.0])
    assert [(w.start_s, w.end_s) for w in observed] == [(0, 1.2), (1.2, 6.5), (6.5, 9), (9, 12)]
    scaled = ad_shot_windows(g, 12.0, [5.0])  # a cut missed: back to the scaled plan
    assert [(w.start_s, w.end_s) for w in scaled] == [(0, 3), (3, 6), (6, 9), (9, 12)]


def test_the_key_shot_of_a_human_prompt_is_its_longest() -> None:
    g = generation()
    assert key_shot_number(g) == 1  # the prompt's own key shot
    g.human_prompt = HumanPrompt(text=HUMAN, shots=parse_prompt_shots(HUMAN))
    longest = max(g.human_prompt.shots, key=lambda s: s.end_s - s.start_s).number
    assert key_shot_number(g) == longest


async def test_the_hook_and_the_key_shot_are_sampled_densely(tmp_path: Path) -> None:
    orch, rt = llm_run(tmp_path, [{"output": verdict_output()}])
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    state = await orch.run_until_blocked(run.id)
    g = state.generation
    assert g is not None and isinstance(g.qa, AdVerdict)
    req = next(r for r in rt._provider_override.requests if r.template == "qa_ad_critic@v3")
    texts = [p.text for p in req.messages[0].parts if hasattr(p, "text")]
    assert sum(1 for t in texts if t.startswith("Image ") and " first two seconds at " in t) == 3
    assert sum(1 for t in texts if t.startswith("Image ") and " shot 3 at " in t) == 8  # key
    assert sum(1 for t in texts if t.startswith("Image ") and " shot 1 at " in t) == 3
    assert g.qa.windows_from == "planned" and g.qa.observed_cuts == []  # the mock has no cuts
    evs = rt.storage.events.tail(state.id, limit=3000)
    windows = [e for e in evs if e.summary.startswith("Critic windows:")]
    assert windows and windows[0].type is EventType.DECISION
    assert "planned 2 / 4 / 6 s" in windows[0].summary


async def test_execution_problems_warn_and_need_a_reason(tmp_path: Path) -> None:
    weak: dict[str, Any] = copy.deepcopy(verdict_output())
    weak["execution"] |= {"product_handled": False, "product_seconds": 1.5, "note": ""}
    explained = copy.deepcopy(weak)
    explained["execution"]["note"] = "Nobody touches the bottle; it is on screen only at the end."
    orch, rt = llm_run(tmp_path, [{"output": weak}, {"output": explained}])
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    state = await orch.run_until_blocked(run.id)
    g = state.generation
    assert g is not None and isinstance(g.qa, AdVerdict)
    assert rt._provider_override.calls_for("qa_ad_critic") == 2  # repaired: the reason was missing
    assert g.status == "approved"  # warn only: the hard gate passed
    assert "the product is never handled by a person" in g.qa.warnings
    assert any(w.startswith("the product is on screen 1.5 s of") for w in g.qa.warnings)
