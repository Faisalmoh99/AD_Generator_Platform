"""P1 (ADR-0027): a whole-ad run on a fake Kie server through the orchestrator: a crash resumes
the persisted task and pays once; an ambiguous submit pauses the run and a resume never
submits that generation again. No network, no real money."""

from __future__ import annotations

import asyncio
import contextlib
import random
from pathlib import Path

import httpx
import pytest

from adsmith.agent.context import AgentRuntime
from adsmith.agent.orchestrator import Orchestrator
from adsmith.agent.recovery.errors import AccountLockedError
from adsmith.domain.enums import Stage
from adsmith.providers.media.kie import KieQueue, KieVideoProvider
from adsmith.storage.account import SpendAccount
from tests.integration.test_multishot_pipeline import REQUEST, events, photo, runtime, settings
from tests.unit.test_kie import BASE, UP, FakeKie


def _make_rt(
    tmp_path: Path, fake: FakeKie, acct: SpendAccount | None = None
) -> tuple[AgentRuntime, KieQueue]:
    s = settings(tmp_path)
    rt = runtime(s)

    async def yield_once(_: float) -> None:
        await asyncio.sleep(0)

    rt.sleep = yield_once
    queue = KieQueue(
        "kie-secret",
        base_url=BASE,
        upload_base_url=UP,
        client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        account=acct,
        poll_interval_s=0.0,
        max_poll_interval_s=0.0,
        timeout_s=1e9,
        sleep=yield_once,
        rng=random.Random(0),
    )
    kie = s.catalog.kie
    assert kie is not None

    async def probe(_: Path) -> tuple[int, int, float]:
        return 480, 854, 8.0

    ep = kie.video.endpoints[kie.video.default]
    rt.ad_provider_factory = lambda run: KieVideoProvider(queue, ep, probe)
    return rt, queue


async def test_killed_during_a_kie_generation_then_resumed_pays_once(tmp_path: Path) -> None:
    fake = FakeKie(polls_running=10**9)
    rt1, q1 = _make_rt(tmp_path, fake)
    orch1 = Orchestrator(rt1)
    run = orch1.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    task = asyncio.ensure_future(orch1.run_until_blocked(run.id, stop_before=Stage.QA))
    while not fake.submitted:  # noqa: ASYNC110 - wait for the paid submit
        await asyncio.sleep(0)
    for _ in range(20):
        await asyncio.sleep(0)
    task.cancel()  # the process dies while Kie renders
    with contextlib.suppress(asyncio.CancelledError):
        await task
    killed = rt1.storage.runs.load(run.id)
    assert killed.generation is not None and killed.generation.status == "generating"
    remote = killed.generation.attempts[0].remote_request
    assert remote is not None and remote.request_id == "task_1"

    fake.polls_running = 0
    rt2, q2 = _make_rt(tmp_path, fake)
    done = await Orchestrator(rt2).resume(run.id, stop_before=Stage.QA)
    g = done.generation
    assert done.stage is Stage.QA and g is not None and g.status == "qa_review"
    assert len(fake.submitted) == 1 and q1.submits == 1 and q2.submits == 0 and q2.resumed == 1
    assert done.budget.reserved_usd == 0
    # settled at the task's own credits (70.2 x $0.005), not at a guess
    assert done.budget.by_category["video"] == pytest.approx(0.351)
    assert any("credits computed" in e for e in events(rt2, run.id))


async def test_an_ambiguous_kie_submit_pauses_and_a_resume_never_resubmits(
    tmp_path: Path,
) -> None:
    acct = SpendAccount(tmp_path / "kie.db", balance_usd=5.0, provider="kie")
    fake = FakeKie(polls_running=0)
    fake.accept_then_lose = 1
    rt1, _ = _make_rt(tmp_path, fake, acct)
    orch1 = Orchestrator(rt1)
    run = orch1.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    with contextlib.suppress(AccountLockedError):
        await orch1.run_until_blocked(run.id, stop_before=Stage.QA)
    assert len(fake.submitted) == 1
    assert acct.summary().reserved_usd > 0  # the wallet keeps the money held (fail-safe)

    rt2, q2 = _make_rt(tmp_path, fake, acct)  # a fresh process: nothing in memory
    with contextlib.suppress(AccountLockedError):
        await Orchestrator(rt2).resume(run.id, stop_before=Stage.QA)
    after = rt2.storage.runs.load(run.id)
    assert len(fake.submitted) == 1 and q2.submits == 0  # never a second paid submit
    assert after.generation is not None and after.generation.status == "failed"
    assert any("nothing is resubmitted automatically" in e for e in events(rt2, run.id))
