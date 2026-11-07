"""Phase B: the whole-ad generation path (multishot, ADR-0023) end to end on zero keys: scripted
LLM (``multishot_en``) + a mock reference-to-video provider (real ffmpeg files with audio), and a
fake Higgsfield server for the crash/resume case. No network, no real money."""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

from adsmith.agent import commands
from adsmith.agent.context import AgentRuntime
from adsmith.agent.edit.service import EditService
from adsmith.agent.orchestrator import Orchestrator
from adsmith.agent.qa import MockCritic
from adsmith.agent.recovery.errors import PermanentProviderError
from adsmith.config import Settings
from adsmith.domain.enums import Stage
from adsmith.domain.events import EventType
from adsmith.domain.formats import AdFormat
from adsmith.domain.qa import QAChecks, QAIssue, QAVerdict
from adsmith.domain.run import RunState
from adsmith.media.ffmpeg import has_audio
from adsmith.providers.llm.scripted import ScriptedLLMProvider
from adsmith.providers.media.higgsfield import HiggsfieldQueue, HiggsfieldVideoProvider
from adsmith.providers.media.mock import MockReferenceVideoProvider
from tests.unit.test_higgsfield import FakeHiggsfield

REQUEST = "Make an 8-second ad for my perfume from this photo."


def photo(tmp_path: Path) -> Path:
    path = tmp_path / "perfume.png"
    Image.new("RGB", (360, 640), (170, 100, 30)).save(path)
    return path


def settings(tmp_path: Path, **over: Any) -> Settings:
    base: dict[str, Any] = {
        "data_dir": tmp_path / "data",
        "account_db": tmp_path / "acct.db",
        "mock_latency_s": 0.0,
        "media_long_side": 320,
    }
    return Settings(**(base | over))


def runtime(s: Settings) -> AgentRuntime:
    llm = ScriptedLLMProvider.from_scenario("multishot_en")
    rt = AgentRuntime.from_settings(s, llm_provider=llm)

    async def no_wait(_: float) -> None:
        return None

    rt.sleep = no_wait
    return rt


def mock_provider(rt: AgentRuntime, run: RunState) -> MockReferenceVideoProvider:
    p = rt.ad_video_provider(run)
    assert isinstance(p, MockReferenceVideoProvider)
    return p


def events(rt: AgentRuntime, run_id: str) -> list[str]:
    return [e.summary for e in rt.storage.events.tail(run_id, limit=2000)]


# ----------------------------------------------------------------------------- happy paths


async def test_auto_mock_run_reaches_done_with_its_own_audio(tmp_path: Path) -> None:
    rt = runtime(settings(tmp_path))
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    assert run.config.scripted_scenario == "multishot_en" and run.product_photo is not None
    state = await orch.run_until_blocked(run.id)
    assert state.stage is Stage.DONE and state.brief is not None
    assert state.brief.format is AdFormat.MULTISHOT
    g = state.generation
    assert g is not None and g.status == "approved" and g.approved_by == "auto_mock"
    # 720x1280 x 8 s x 24 / 1024 = 172,800 tokens x $0.014/1k = $2.4192 (margin 0: reserved)
    assert g.computed_usd == pytest.approx(2.4192) and len(g.prompt.shots) == 4
    assert g.est_usd == pytest.approx(2.4192) and g.price_basis is not None
    assert "172,800 tokens" in g.price_basis
    assert g.prompt.text.startswith("An 8-second vertical 9:16 film in 4 shots")
    assert "No text, letters" in g.prompt.text
    assert mock_provider(rt, state).calls == 1
    assert state.storyboard is not None
    assert all(sc.prompt is None and sc.keyframe is None for sc in state.storyboard.scenes)
    final = rt.storage.artifacts.run_dir(run.id) / "final.mp4"
    assert await has_audio(final)  # the generation's own sound is kept
    assert state.final is not None and state.final.duration_s == pytest.approx(10.0, abs=0.15)
    assert state.budget.by_category["video"] == pytest.approx(2.4192)  # settled at the price
    check = [e for e in rt.storage.events.tail(run.id, limit=3000) if "price_check" in e.payload]
    assert len(check) == 1 and check[0].summary.startswith("Price check: computed $2.419")
    assert "output 180x320" in check[0].summary  # the real (mock) file size, from the provider
    assert state.budget.reserved_usd == 0
    pack = rt.storage.artifacts.run_dir(run.id) / "production_pack" / "storyboard.json"
    assert json.loads(pack.read_text())["whole_ad_generation"]["status"] == "approved"


async def test_interactive_run_waits_at_the_estimate_gate_then_generates(tmp_path: Path) -> None:
    rt = runtime(settings(tmp_path))
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=True, product_photo=photo(tmp_path))
    state = await orch.run_until_blocked(run.id)
    assert state.stage is Stage.AWAITING_CONCEPT_CHOICE
    commands.choose_concept(rt.storage, run.id, 1)
    state = await orch.run_until_blocked(run.id)
    assert state.stage is Stage.AWAITING_GENERATION_APPROVAL
    g = state.generation
    assert g is not None and g.est_usd == pytest.approx(2.4192) and not g.approved
    assert mock_provider(rt, state).calls == 0  # nothing generated before the approval
    assert state.budget.by_category.get("video", 0) == 0
    assert any("Waiting for a human to approve $2.419" in m for m in events(rt, run.id))
    assert any(m.startswith("Estimate: $2.419 for one generation") for m in events(rt, run.id))
    commands.approve_generation(rt.storage, run.id)
    state = await orch.run_until_blocked(run.id)
    assert state.stage is Stage.DONE and state.generation is not None
    assert state.generation.approved_by == "user"


async def test_real_money_stops_at_the_gate_even_in_auto_mode(tmp_path: Path) -> None:
    s = settings(tmp_path, ad_provider="higgsfield", hf_key="kid:secret", hf_balance_usd=5.0)
    rt = runtime(s)
    rt.ad_provider_factory = lambda run: MockReferenceVideoProvider(
        model="fake-higgsfield",
        usd_per_second=0.3,
        max_prompt_chars=2500,
        latency_s=0,
        sleep=rt.sleep,
    )
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    state = await orch.run_until_blocked(run.id)
    assert state.stage is Stage.AWAITING_GENERATION_APPROVAL
    assert state.budget.media_limit_usd == pytest.approx(5.0)  # the Higgsfield wallet


async def test_declined_estimate_spends_nothing(tmp_path: Path) -> None:
    rt = runtime(settings(tmp_path))
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=True, product_photo=photo(tmp_path))
    await orch.run_until_blocked(run.id)
    commands.choose_concept(rt.storage, run.id, 1)
    await orch.run_until_blocked(run.id)
    state = commands.decline_generation(rt.storage, run.id, reason="too expensive")
    assert state.stage is Stage.DONE_PARTIAL and state.generation is not None
    assert state.generation.status == "failed" and mock_provider(rt, state).calls == 0
    assert state.budget.by_category.get("video", 0) == 0


# ----------------------------------------------------------------------------- refusals / failures


async def test_an_estimate_above_the_run_budget_is_refused_before_any_spend(tmp_path: Path) -> None:
    rt = runtime(settings(tmp_path, budget_usd=1.0))  # the generation is estimated at $3.60
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    state = await orch.run_until_blocked(run.id)
    assert state.stage is Stage.DONE_PARTIAL and state.final is None
    g = state.generation
    assert g is not None and g.status == "failed" and g.failure is not None
    assert "exceeds the remaining" in g.failure.reason_debug
    assert mock_provider(rt, state).calls == 0 and state.budget.reserved_usd == 0


async def test_a_failed_generation_is_not_retried_with_new_money(tmp_path: Path) -> None:
    rt = runtime(settings(tmp_path))
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    provider = mock_provider(rt, run)
    provider.fail_with = PermanentProviderError("upstream failed (not charged)")
    state = await orch.run_until_blocked(run.id)
    assert state.stage is Stage.DONE_PARTIAL and provider.calls == 1
    g = state.generation
    assert g is not None and g.status == "failed" and state.budget.reserved_usd == 0
    assert state.budget.by_category.get("video", 0) == 0


async def test_a_flagged_ad_is_assembled_and_waits_for_a_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = runtime(settings(tmp_path))
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))

    async def flagging(self: MockCritic, state: RunState) -> tuple[QAVerdict, list[Any]]:
        checks = QAChecks(
            product_visible=True,
            matches_shot_spec=True,
            lighting_matches=True,
            continuity_ok=False,
            artifacts_free=True,
            brand_safe=True,
        )
        issue = QAIssue(
            category="product",
            severity="major",
            observation="the cap turns silver in shot 3",
            fix="keep the gold cap",
        )
        verdict = QAVerdict(
            score=0.55, checks=checks, issues=[issue], frames_inspected=13, threshold=0.7
        )
        return verdict, []

    monkeypatch.setattr(MockCritic, "review_ad", flagging)
    state = await orch.run_until_blocked(run.id)
    assert state.stage is Stage.DONE_PARTIAL and state.final is not None  # warn only: assembled
    g = state.generation
    assert g is not None and g.status == "flagged" and mock_provider(rt, state).calls == 1
    assert any("FAILED the hard gate" in m for m in events(rt, run.id))
    state = commands.accept_ad(rt.storage, run.id, reason="the cap reads gold at full size")
    assert state.stage is Stage.DONE and state.generation is not None
    assert state.generation.status == "approved" and state.generation.qa is not None
    assert state.generation.qa.score == 0.55  # the failing verdict stays as the record


async def test_regenerate_goes_back_through_the_estimate(tmp_path: Path) -> None:
    rt = runtime(settings(tmp_path))
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    first = await orch.run_until_blocked(run.id)
    assert first.generation is not None and first.final is not None
    state = commands.regenerate_ad(rt.storage, run.id, reason="try another take")
    assert state.stage is Stage.ESTIMATE and state.final is None
    state = await orch.run_until_blocked(run.id)
    g = state.generation
    assert state.stage is Stage.DONE and g is not None and g.version == 2
    assert len(g.history) == 1 and g.history[0].artifact == first.generation.artifact
    assert mock_provider(rt, state).calls == 2  # a new request: version is part of the key
    assert g.artifact != first.generation.artifact


async def test_edits_on_a_whole_ad_run_are_rejected_with_a_clear_message(tmp_path: Path) -> None:
    rt = runtime(settings(tmp_path))
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    await orch.run_until_blocked(run.id)
    outcome = await EditService(rt).edit(run.id, "make scene 2 slower")
    assert outcome.status == "rejected" and "regenerate-ad" in outcome.message
    assert rt.storage.runs.load(run.id).stage is Stage.DONE


def test_multishot_needs_a_photo(tmp_path: Path) -> None:
    orch = Orchestrator(runtime(settings(tmp_path)))
    with pytest.raises(ValueError, match="product photo"):
        orch.create_run(REQUEST, ad_format=AdFormat.MULTISHOT)


async def test_scene_based_runs_are_untouched(tmp_path: Path) -> None:
    """No photo: the narrative path runs keyframes and clips as before; no generation object."""
    s = settings(tmp_path)
    rt = AgentRuntime.from_settings(s, llm_provider=ScriptedLLMProvider.from_scenario("perfume_en"))
    orch = Orchestrator(rt)
    run = orch.create_run("perfume ad", interactive=False)
    state = await orch.run_until_blocked(run.id, stop_before=Stage.KEYFRAME)
    assert state.stage is Stage.KEYFRAME and state.generation is None


# ----------------------------------------------------------------------------- crash safety


async def test_killed_during_the_generation_then_resumed_pays_once(tmp_path: Path) -> None:
    s = settings(tmp_path)
    fake = FakeHiggsfield(polls_in_progress=10**9)  # the job keeps rendering until released

    def make_rt() -> tuple[AgentRuntime, HiggsfieldQueue]:
        rt = runtime(s)

        async def yield_once(_: float) -> None:
            await asyncio.sleep(0)

        rt.sleep = yield_once
        queue = HiggsfieldQueue(
            "kid:secret",
            base_url="https://api.higgsfield.ai",
            client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
            poll_interval_s=0.0,
            max_poll_interval_s=0.0,
            timeout_s=1e9,
            sleep=yield_once,
            rng=random.Random(0),
        )
        catalog = s.catalog.higgsfield
        assert catalog is not None

        async def probe(_: Path) -> tuple[int, int, float]:
            return 720, 1280, 8.0

        ep = catalog.video.endpoints[catalog.video.default]
        rt.ad_provider_factory = lambda run: HiggsfieldVideoProvider(queue, ep, probe)
        return rt, queue

    rt1, q1 = make_rt()
    orch1 = Orchestrator(rt1)
    run = orch1.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    task = asyncio.ensure_future(orch1.run_until_blocked(run.id, stop_before=Stage.QA))
    while not fake.submitted:  # noqa: ASYNC110 - wait for the paid submit
        await asyncio.sleep(0)
    for _ in range(20):
        await asyncio.sleep(0)
    task.cancel()  # the process dies while Higgsfield renders
    with contextlib.suppress(asyncio.CancelledError):
        await task
    killed = rt1.storage.runs.load(run.id)
    assert killed.generation is not None and killed.generation.status == "generating"
    assert killed.generation.attempts[0].remote_request is not None  # persisted before polling

    fake.polls_in_progress = 0  # it finished while we were down
    rt2, q2 = make_rt()  # a fresh process: nothing in memory
    done = await Orchestrator(rt2).resume(run.id, stop_before=Stage.QA)
    g = done.generation
    assert done.stage is Stage.QA and g is not None and g.status == "qa_review"
    assert len(fake.submitted) == 1 and q1.submits == 1 and q2.submits == 0 and q2.resumed == 1
    assert done.budget.reserved_usd == 0
    assert done.budget.by_category["video"] == pytest.approx(g.approved_usd or 0)
    assert any(
        e.type is EventType.DECISION and "interrupted whole-ad generation" in e.summary
        for e in rt2.storage.events.tail(run.id, limit=2000)
    )


async def test_the_studio_view_shows_the_new_stages_and_the_whole_ad(tmp_path: Path) -> None:
    from adsmith.api.view import run_view  # noqa: PLC0415

    rt = runtime(settings(tmp_path))
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=True, product_photo=photo(tmp_path))
    await orch.run_until_blocked(run.id)
    commands.choose_concept(rt.storage, run.id, 1)
    state = await orch.run_until_blocked(run.id)
    v = run_view(state, rt.storage)
    stages = [p["stage"] for p in v["pipeline"]]
    assert stages.index("insight") == stages.index("understand") + 1
    assert stages.index("director") == stages.index("prompt") + 1
    assert stages.index("props") == stages.index("director") + 1
    assert stages.index("estimate") == stages.index("props") + 1
    active = [p["stage"] for p in v["pipeline"] if p["state"] == "active"]
    assert active == ["estimate"]  # the spend gate sits on the estimate
    g = v["generation"]
    assert g["status"] == "estimated" and g["est_usd"] == pytest.approx(2.4192)
    assert g["computed_usd"] == pytest.approx(2.4192)
    assert g["prompt"].startswith("An 8-second") and g["approved_by"] is None
    c = v["concepts"][0]
    assert (
        set(c["rubric"])
        == {"insight", "hook", "story_turn", "originality", "cultural_meaning", "product_role"}
        and c["product_role"]["role"] == "catalyst"
    )
    assert v["insights"] and v["insights"][0]["statement"].startswith("In the Gulf")


def test_a_multishot_run_defaults_to_the_endpoint_length_not_the_montage_one(
    tmp_path: Path,
) -> None:
    rt = runtime(settings(tmp_path))
    orch = Orchestrator(rt)
    run = orch.create_run("An ad for my perfume.", interactive=False, product_photo=photo(tmp_path))
    assert run.config.target_duration_s == 8.0  # the catalog's default_duration_s
    plain = orch.create_run("An ad for my perfume.", interactive=False)
    assert plain.config.target_duration_s == 15.0  # montage / narrative: unchanged
    rt2 = runtime(settings(tmp_path / "b", target_duration_s=10.0))
    again = Orchestrator(rt2).create_run("An ad.", interactive=False, product_photo=photo(tmp_path))
    assert again.config.target_duration_s == 10.0  # an explicit setting wins
