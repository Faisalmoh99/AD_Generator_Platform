"""Before try 2: the observed (console) charge can be booked, a multishot final keeps the
generation's size, prop reference images are sent in order and checked as props, and a 9 s
human prompt with fractional timecodes is accepted."""

from __future__ import annotations

import asyncio
import random
import subprocess
from pathlib import Path

import httpx
import pytest
from PIL import Image

from adsmith.agent import commands
from adsmith.agent.commands import CommandRejectedError
from adsmith.agent.context import AgentRuntime
from adsmith.agent.orchestrator import Orchestrator
from adsmith.domain.ad import human_prompt_problems, parse_prompt_shots
from adsmith.domain.budget import BudgetLedger
from adsmith.domain.enums import Stage
from adsmith.providers.llm.scripted import ScriptedLLMProvider
from adsmith.providers.media.higgsfield import HiggsfieldQueue, HiggsfieldVideoProvider
from adsmith.providers.media.mock import MockReferenceVideoProvider
from adsmith.storage.account import SpendAccount
from tests.integration.test_ad_critic import verdict_output
from tests.integration.test_multishot_pipeline import (
    REQUEST,
    mock_provider,
    photo,
    runtime,
    settings,
)
from tests.unit.test_higgsfield import FakeHiggsfield


def prop(tmp_path: Path) -> Path:
    p = tmp_path / "mabkhara.png"
    Image.new("RGB", (64, 48), (90, 60, 30)).save(p)
    return p


# ----------------------------------------------------------------------------- observed charge


def test_the_budget_and_the_ledger_book_the_observed_charge(tmp_path: Path) -> None:
    b = BudgetLedger(limit_usd=5, max_attempts_per_scene=3, max_total_attempts=10)
    b.reserve("att1", "video", 2.4192, None)
    b.settle("att1", 2.4192)
    assert b.correct("att1", 2.06) == pytest.approx(2.4192)
    assert b.spent_usd == pytest.approx(2.06) and b.by_category["video"] == pytest.approx(2.06)
    with pytest.raises(KeyError):
        b.correct("nope", 1.0)
    acct = SpendAccount(tmp_path / "a.db", balance_usd=5.0, provider="higgsfield")
    sid = acct.reserve(endpoint="ep", est_usd=2.4192, ref="ad v1")
    acct.settle(sid, 2.4192, remote_id="req1")
    assert acct.correct_actual("req1", 2.06) == pytest.approx(2.4192)
    assert acct.summary().remaining_usd == pytest.approx(2.94)
    with pytest.raises(KeyError):
        acct.correct_actual("req-unknown", 1.0)
    other = SpendAccount(tmp_path / "a.db", balance_usd=5.0, provider="fal")
    with pytest.raises(KeyError):  # another provider's rows are never touched
        other.correct_actual("req1", 0.0)


async def test_settle_actual_corrects_the_ledger_run_and_attempt(tmp_path: Path) -> None:
    s = settings(tmp_path)
    rt = runtime(s)
    acct = SpendAccount(s.account_db, balance_usd=5.0, provider="higgsfield")
    fake = FakeHiggsfield(polls_in_progress=0)

    async def yield_once(_: float) -> None:
        await asyncio.sleep(0)

    queue = HiggsfieldQueue(
        "kid:secret",
        base_url="https://api.higgsfield.ai",
        client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        account=acct,
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
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    state = await orch.run_until_blocked(run.id, stop_before=Stage.QA)
    assert state.generation is not None and state.generation.status == "qa_review"
    assert acct.summary().settled_usd == pytest.approx(1.80)  # the fake's /estimate price

    state = commands.settle_actual(rt.storage, acct, run.id, usd=1.50)
    assert acct.summary().settled_usd == pytest.approx(1.50)
    assert acct.summary().remaining_usd == pytest.approx(3.50)
    assert state.budget.by_category["video"] == pytest.approx(1.50)
    assert state.generation is not None and state.generation.attempts[-1].cost_usd == 1.50
    ev = next(
        e
        for e in rt.storage.events.tail(run.id, limit=3000)
        if e.summary.startswith("HUMAN: observed charge")
    )
    assert ev.payload["observed_usd"] == 1.50 and ev.payload["booked_usd"] == pytest.approx(1.80)
    with pytest.raises(CommandRejectedError, match="negative"):
        commands.settle_actual(rt.storage, acct, run.id, usd=-1)


async def test_settle_actual_refuses_a_run_without_a_paid_attempt(tmp_path: Path) -> None:
    orch = Orchestrator(runtime(settings(tmp_path)))
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    await orch.run_until_blocked(run.id)  # mock: no remote request
    acct = SpendAccount(tmp_path / "x.db", balance_usd=5.0, provider="higgsfield")
    with pytest.raises(CommandRejectedError, match="no paid, successful attempt"):
        commands.settle_actual(orch.rt.storage, acct, run.id, usd=1.0)


# ----------------------------------------------------------------------------- export size


async def test_a_multishot_final_keeps_the_generation_size(tmp_path: Path) -> None:
    s = settings(tmp_path)  # media_long_side 320: the old export size
    rt = runtime(s)
    rt.ad_provider_factory = lambda run: MockReferenceVideoProvider(
        model="mock-640", usd_per_second=0.3, max_prompt_chars=2500, latency_s=0,
        sleep=rt.sleep, long_side=640,
    )  # fmt: skip
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    state = await orch.run_until_blocked(run.id)
    assert state.stage is Stage.DONE and state.generation is not None
    art = state.generation.artifact
    assert art is not None and (art.width, art.height) == (360, 640)
    final = rt.storage.artifacts.run_dir(run.id) / "final.mp4"
    out = subprocess.run(  # noqa: ASYNC221 - a one-off probe in a test
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0", str(final)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip
    assert out == "360,640"  # not scaled to the 320 long side


# ----------------------------------------------------------------------------- prop references


async def test_reference_images_go_after_the_product_in_order(tmp_path: Path) -> None:
    rt = runtime(settings(tmp_path))
    orch = Orchestrator(rt)
    run = orch.create_run(
        REQUEST, interactive=True, product_photo=photo(tmp_path), ref_images=[prop(tmp_path)]
    )
    assert [r.name for r in run.reference_images] == ["mabkhara.png"]
    await orch.run_until_blocked(run.id)
    commands.choose_concept(rt.storage, run.id, 1)
    state = await orch.run_until_blocked(run.id)
    assert state.stage is Stage.AWAITING_GENERATION_APPROVAL
    est = next(
        e for e in rt.storage.events.tail(run.id, limit=3000) if e.summary.startswith("Estimate:")
    )
    urls = est.payload["image_urls"]
    # The product photo, the user's prop, then the two props the prompt names (ADR-0026).
    assert isinstance(urls, list) and len(urls) == 4
    m = mock_provider(rt, state)
    assert state.product_photo is not None
    assert m._urls[str(urls[0])].stem == state.product_photo.sha256  # Image 1: the product
    assert m._urls[str(urls[1])].stem == state.reference_images[0].artifact.sha256  # Image 2
    assert [r.source for r in state.reference_images] == ["user", "generated", "generated"]


def test_reference_images_need_a_product_photo_and_must_exist(tmp_path: Path) -> None:
    orch = Orchestrator(runtime(settings(tmp_path)))
    with pytest.raises(ValueError, match="with a product photo"):
        orch.create_run(REQUEST, interactive=False, ref_images=[prop(tmp_path)])
    with pytest.raises(ValueError, match="no reference image"):
        orch.create_run(
            REQUEST, interactive=False, product_photo=photo(tmp_path),
            ref_images=[tmp_path / "missing.png"],
        )  # fmt: skip


async def test_a_prop_changes_the_cache_key(tmp_path: Path) -> None:
    from adsmith.agent.generation_runner import _ref_shas  # noqa: PLC0415

    orch = Orchestrator(runtime(settings(tmp_path)))
    a = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    b = orch.create_run(
        REQUEST, interactive=False, product_photo=photo(tmp_path), ref_images=[prop(tmp_path)]
    )
    assert _ref_shas(a) == () and len(_ref_shas(b)) == 1


async def test_the_critic_sees_the_prop_as_a_prop(tmp_path: Path) -> None:
    rt = runtime(settings(tmp_path, qa_mode="llm"))
    llm = rt._provider_override
    assert isinstance(llm, ScriptedLLMProvider)
    llm.override("qa_ad_critic", [{"output": verdict_output()}])
    orch = Orchestrator(rt)
    run = orch.create_run(
        REQUEST, interactive=False, product_photo=photo(tmp_path), ref_images=[prop(tmp_path)]
    )
    state = await orch.run_until_blocked(run.id)
    assert state.stage is Stage.DONE and state.generation is not None
    # The photo, three props (the user's + two generated), 3 hook frames, 3 shots × 3 frames and
    # the key shot × 8 (ADR-0026 phase 5).
    assert state.generation.qa is not None and state.generation.qa.frames_inspected == 24
    req = next(r for r in llm.requests if r.template == "qa_ad_critic@v5")
    msg = req.messages[0]
    assert hasattr(msg, "parts")
    texts = [p.text for p in msg.parts if hasattr(p, "text")]
    assert texts[0] == "Image 0: the real product photo (the reference)"
    assert texts[1] == "Image 1: prop reference (mabkhara.png), a prop and NOT the product"
    body = "\n".join(texts)
    assert (
        "Prop references (props, not the product): image 1 (mabkhara.png), image 2 (brass "
        "tray), image 3 (coffee cup)." in body
    )


def test_the_critic_template_has_no_prop_line_without_props(tmp_path: Path) -> None:
    from adsmith.agent.prompts import PromptLibrary  # noqa: PLC0415

    t = PromptLibrary(settings(tmp_path).prompts_dir).get("qa_ad_critic")
    assert t.version == 5 and "props" in t.variables


# ----------------------------------------------------------------------------- 9 s prompt


def test_a_nine_second_prompt_with_fractional_timecodes_is_accepted() -> None:
    text = (
        "Look: x.\n"
        "Shot 1 (0-2 s): a.\nShot 2 (2-4 s): b.\nShot 3 (4-6.5 s): c.\nShot 4 (6.5-9 s): d.\n"
    )
    assert human_prompt_problems(text, duration_s=9, max_chars=2500) == []
    assert [(s.start_s, s.end_s) for s in parse_prompt_shots(text)][2:] == [(4, 6.5), (6.5, 9)]
    assert "not at 8 s" in human_prompt_problems(text, duration_s=8, max_chars=2500)[0]


def test_a_nine_second_setting_reaches_a_multishot_run(tmp_path: Path) -> None:
    rt = AgentRuntime.from_settings(
        settings(tmp_path, target_duration_s=9.0),
        llm_provider=ScriptedLLMProvider.from_scenario("multishot_en"),
    )
    run = Orchestrator(rt).create_run("an ad", interactive=False, product_photo=photo(tmp_path))
    assert run.config.target_duration_s == 9.0
