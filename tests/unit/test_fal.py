"""M11: fal queue client, providers, adapters and the account (wallet) ledger. No network:
``httpx.MockTransport`` plays fal's queue API (shapes from the verified OpenAPI schemas)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from adsmith.agent.context import AgentRuntime, video_capabilities
from adsmith.agent.orchestrator import Orchestrator
from adsmith.agent.recovery.errors import (
    BudgetExceededError,
    ContentPolicyError,
    PermanentProviderError,
    RateLimitedError,
    TransientProviderError,
)
from adsmith.config import Settings
from adsmith.domain.enums import ScenePhase
from adsmith.domain.scene import ScenePrompt
from adsmith.providers.adapters.base import ADAPTERS
from adsmith.providers.llm.scripted import ScriptedLLMProvider
from adsmith.providers.media.base import ImageJob, MediaLabel, VideoJob
from adsmith.providers.media.fal import FalImageProvider, FalQueue, FalVideoProvider
from adsmith.storage.account import AccountLimitError, SpendAccount

FLUX = "fal-ai/flux/dev"
CDN = "https://v3.fal.media/files/x"


def settings(tmp_path: Path, **over: Any) -> Settings:
    base: dict[str, Any] = {"data_dir": tmp_path / "data", "account_db": tmp_path / "acct.db"}
    return Settings(**(base | over))


class FakeFal:
    """Scriptable fake of the queue API. ``faults`` maps a step name to a list of responses
    (or exceptions) consumed before the normal behaviour resumes."""

    def __init__(self, result: dict[str, Any], *, polls_in_progress: int = 1) -> None:
        self.result = result
        self.polls_in_progress = polls_in_progress
        self.faults: dict[str, list[httpx.Response | Exception]] = {}
        self.submitted: list[dict[str, Any]] = []
        self.paths: list[str] = []
        self._polls = 0

    def _fault(self, step: str) -> httpx.Response | None:
        queue = self.faults.get(step)
        if not queue:
            return None
        f = queue.pop(0)
        if isinstance(f, Exception):
            raise f
        return f

    def handler(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911 - one route each
        url = str(request.url)
        self.paths.append(f"{request.method} {url}")
        if request.method == "POST":
            if (f := self._fault("submit")) is not None:
                return f
            self.submitted.append(json.loads(request.content))
            rid = f"req{len(self.submitted)}"
            return httpx.Response(
                200,
                json={
                    "request_id": rid,
                    "status_url": f"https://queue.fal.run/x/requests/{rid}/status",
                    "response_url": f"https://queue.fal.run/x/requests/{rid}",
                },
            )
        if url.endswith("/status"):
            if (f := self._fault("status")) is not None:
                return f
            self._polls += 1
            done = self._polls > self.polls_in_progress
            return httpx.Response(200, json={"status": "COMPLETED" if done else "IN_PROGRESS"})
        if url.startswith(CDN):
            if (f := self._fault("download")) is not None:
                return f
            return httpx.Response(200, content=b"\x89PNG fake bytes")
        if (f := self._fault("result")) is not None:
            return f
        return httpx.Response(200, json=self.result)


def make_queue(fake: FakeFal, account: SpendAccount | None = None) -> tuple[FalQueue, list[float]]:
    sleeps: list[float] = []

    async def no_wait(s: float) -> None:
        sleeps.append(s)

    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    t = iter(range(10_000))
    queue = FalQueue(
        "test-key",
        client=client,
        account=account,
        poll_interval_s=1.0,
        timeout_s=30.0,
        sleep=no_wait,
        clock=lambda: float(next(t)),
    )
    return queue, sleeps


def label(phase: ScenePhase = ScenePhase.KEYFRAME) -> MediaLabel:
    return MediaLabel(scene_order=4, scene_version=1, phase=phase, attempt_no=1)


def image_job() -> ImageJob:
    return ImageJob("a faceted amber bottle", "text, watermark", 720, 1280, 0, label())


IMAGE_RESULT = {
    "images": [{"url": f"{CDN}/kf.png", "width": 720, "height": 1280}],
    "seed": 0,
    "has_nsfw_concepts": [False],
    "prompt": "x",
    "timings": {},
}


def flux(s: Settings, queue: FalQueue) -> FalImageProvider:
    return FalImageProvider(queue, s.catalog.fal.image.endpoints["flux_dev"])


def video(s: Settings, queue: FalQueue, key: str) -> FalVideoProvider:
    ep = s.catalog.fal.video.endpoints[key]

    async def probe(p: Path) -> tuple[int, int, float]:
        return 1080, 1920, 6.04

    return FalVideoProvider(queue, ep, video_capabilities(ep), probe)


def account(tmp_path: Path, balance: float = 10.0) -> SpendAccount:
    return SpendAccount(tmp_path / "acct.db", balance_usd=balance)


# ----------------------------------------------------------------------------- catalog / adapters


def test_catalog_prices_are_the_verified_ones(tmp_path: Path) -> None:
    fal = settings(tmp_path).catalog.fal
    flux_ep = fal.image.endpoints["flux_dev"]
    assert flux_ep.endpoint == FLUX
    assert flux_ep.usd_for(720, 1280) == pytest.approx(0.025)  # 0.92 MP → 1 MP billed
    assert flux_ep.usd_for(1080, 1920) == pytest.approx(0.075)  # 2.07 MP → 3 MP billed
    ltx = fal.video.endpoints["ltx_23_fast"]
    assert ltx.endpoint == "fal-ai/ltx-2.3/image-to-video/fast" and ltx.usd_per_second == 0.06
    kling = fal.video.endpoints["kling_v3_pro"]
    assert kling.image_field == "start_image_url" and kling.usd_per_second == 0.14


def _prompt() -> ScenePrompt:
    return ScenePrompt(
        keyframe_prompt="k" * 50,
        motion_prompt="Camera pushes in slowly toward the bottle.",
        negative_prompt="text, watermark",
        template_version="prompt_writer@v2",
        bible_version=1,
    )


def test_ltx_adapter_snaps_to_six_seconds_and_drops_negative(tmp_path: Path) -> None:
    caps = video_capabilities(settings(tmp_path).catalog.fal.video.endpoints["ltx_23_fast"])
    out = ADAPTERS["ltx"].adapt(_prompt(), 3.5, "9:16", caps)
    assert out.duration_s == 6
    assert out.negative_prompt == ""
    assert out.params == {
        "duration": 6,
        "fps": 24,
        "resolution": "1080p",
        "generate_audio": False,
        "aspect_ratio": "9:16",
    }


def test_kling_adapter_uses_string_duration_and_audio_off(tmp_path: Path) -> None:
    caps = video_capabilities(settings(tmp_path).catalog.fal.video.endpoints["kling_v3_pro"])
    out = ADAPTERS["kling"].adapt(_prompt(), 3.5, "9:16", caps)
    assert out.duration_s == 4
    assert out.params == {"duration": "4", "cfg_scale": 0.5, "generate_audio": False}
    assert out.negative_prompt == "text, watermark"


# ----------------------------------------------------------------------------- happy paths


async def test_image_submit_poll_result_download_and_settle(tmp_path: Path) -> None:
    s, acct = settings(tmp_path), account(tmp_path)
    fake = FakeFal(IMAGE_RESULT, polls_in_progress=2)
    queue, sleeps = make_queue(fake, acct)
    out = await flux(s, queue).generate_image(image_job(), tmp_path)
    assert out.path.read_bytes().startswith(b"\x89PNG")
    assert out.cost_usd == pytest.approx(0.025) and out.meta["request_id"] == "req1"
    body = fake.submitted[0]
    assert body["image_size"] == {"width": 720, "height": 1280}
    assert body["seed"] == 0 and body["num_images"] == 1
    assert "negative_prompt" not in body  # FLUX dev has no such field
    assert fake.paths[0] == f"POST https://queue.fal.run/{FLUX}"
    assert sleeps == [1.0, 1.0]
    summary = acct.summary()
    assert summary.settled_usd == pytest.approx(0.025) and summary.reserved_usd == 0


@pytest.mark.parametrize(
    ("key", "image_field", "negative"),
    [("ltx_23_fast", "image_url", False), ("kling_v3_pro", "start_image_url", True)],
)
async def test_video_payload_matches_the_verified_schema(
    tmp_path: Path, key: str, image_field: str, negative: bool
) -> None:
    s, acct = settings(tmp_path), account(tmp_path)
    kf = tmp_path / "kf.png"
    kf.write_bytes(b"\x89PNG keyframe")
    fake = FakeFal({"video": {"url": f"{CDN}/clip.mp4"}})
    queue, _ = make_queue(fake, acct)
    provider = video(s, queue, key)
    adapted = ADAPTERS[provider.endpoint.adapter].adapt(
        _prompt(), 3.5, "9:16", provider.capabilities
    )
    job = VideoJob(
        kf,
        adapted.prompt,
        adapted.negative_prompt,
        adapted.duration_s,
        1080,
        1920,
        adapted.params,
        label(ScenePhase.CLIP),
    )
    out = await provider.image_to_video(job, tmp_path)
    body = fake.submitted[0]
    assert body[image_field].startswith("data:image/png;base64,")
    assert body["prompt"] == _prompt().motion_prompt
    assert ("negative_prompt" in body) is negative
    assert body["generate_audio"] is False
    rate = provider.usd_per_second
    assert out.cost_usd == pytest.approx(rate * adapted.duration_s)
    assert out.duration_s == 6.04 and out.path.suffix == ".mp4"
    assert acct.summary().settled_usd == pytest.approx(out.cost_usd)


# ----------------------------------------------------------------------------- no double spend


@pytest.mark.parametrize("step", ["status", "result", "download"])
async def test_transient_after_submit_resumes_instead_of_resubmitting(
    tmp_path: Path, step: str
) -> None:
    s, acct = settings(tmp_path), account(tmp_path)
    fake = FakeFal(IMAGE_RESULT, polls_in_progress=0)
    fake.faults[step] = [httpx.Response(503, text="upstream unavailable")]
    queue, _ = make_queue(fake, acct)
    provider = flux(s, queue)
    with pytest.raises(TransientProviderError):
        await provider.generate_image(image_job(), tmp_path)
    assert acct.summary().reserved_usd == pytest.approx(0.025)  # still in flight
    out = await provider.generate_image(image_job(), tmp_path)  # the recovery layer's retry
    assert queue.submits == 1 and len(fake.submitted) == 1
    assert out.meta["request_id"] == "req1"
    summary = acct.summary()
    assert summary.settled_usd == pytest.approx(0.025) and summary.reserved_usd == 0


async def test_poll_timeout_keeps_the_request_for_resumption(tmp_path: Path) -> None:
    s = settings(tmp_path)
    fake = FakeFal(IMAGE_RESULT, polls_in_progress=10_000)
    queue, _ = make_queue(fake)
    with pytest.raises(TransientProviderError, match="kept"):
        await flux(s, queue).generate_image(image_job(), tmp_path)
    fake.polls_in_progress = 0
    await flux(s, queue).generate_image(image_job(), tmp_path)
    assert queue.submits == 1


async def test_a_different_request_is_a_new_submission(tmp_path: Path) -> None:
    s = settings(tmp_path)
    fake = FakeFal(IMAGE_RESULT, polls_in_progress=0)
    queue, _ = make_queue(fake)
    await flux(s, queue).generate_image(image_job(), tmp_path)
    other = ImageJob("a faceted amber bottle", "", 720, 1280, 1, label())  # new seed
    await flux(s, queue).generate_image(other, tmp_path)
    assert queue.submits == 2


# ----------------------------------------------------------------------------- error mapping


async def test_safety_rejection_on_submit_is_content_policy_and_released(tmp_path: Path) -> None:
    s, acct = settings(tmp_path), account(tmp_path)
    fake = FakeFal(IMAGE_RESULT)
    detail = {"detail": [{"type": "content_policy_violation", "msg": "flagged"}]}
    fake.faults["submit"] = [httpx.Response(422, json=detail)]
    queue, _ = make_queue(fake, acct)
    with pytest.raises(ContentPolicyError):
        await flux(s, queue).generate_image(image_job(), tmp_path)
    assert acct.summary().reserved_usd == 0 and acct.summary().settled_usd == 0


async def test_nsfw_flag_is_content_policy_and_still_billed(tmp_path: Path) -> None:
    s, acct = settings(tmp_path), account(tmp_path)
    flagged = IMAGE_RESULT | {"has_nsfw_concepts": [True]}
    queue, _ = make_queue(FakeFal(flagged, polls_in_progress=0), acct)
    with pytest.raises(ContentPolicyError):
        await flux(s, queue).generate_image(image_job(), tmp_path)
    assert acct.summary().settled_usd == pytest.approx(0.025)


async def test_rate_limit_carries_retry_after(tmp_path: Path) -> None:
    s = settings(tmp_path)
    fake = FakeFal(IMAGE_RESULT)
    fake.faults["submit"] = [httpx.Response(429, headers={"retry-after": "7"}, text="slow down")]
    queue, _ = make_queue(fake)
    with pytest.raises(RateLimitedError) as info:
        await flux(s, queue).generate_image(image_job(), tmp_path)
    assert info.value.retry_after_s == 7.0


async def test_bad_request_is_permanent_and_released(tmp_path: Path) -> None:
    s, acct = settings(tmp_path), account(tmp_path)
    fake = FakeFal(IMAGE_RESULT, polls_in_progress=0)
    fake.faults["result"] = [httpx.Response(422, json={"detail": "duration must be one of 6,8"})]
    queue, _ = make_queue(fake, acct)
    with pytest.raises(PermanentProviderError):
        await flux(s, queue).generate_image(image_job(), tmp_path)
    assert acct.summary().reserved_usd == 0 and acct.summary().settled_usd == 0


async def test_ambiguous_submit_timeout_keeps_the_reservation(tmp_path: Path) -> None:
    s, acct = settings(tmp_path), account(tmp_path)
    fake = FakeFal(IMAGE_RESULT)
    fake.faults["submit"] = [httpx.ReadTimeout("no response")]
    queue, _ = make_queue(fake, acct)
    with pytest.raises(TransientProviderError, match="reservation kept"):
        await flux(s, queue).generate_image(image_job(), tmp_path)
    assert acct.summary().reserved_usd == pytest.approx(0.025)


# ----------------------------------------------------------------------------- wallet ceiling


async def test_account_ceiling_refuses_before_any_http_request(tmp_path: Path) -> None:
    s, acct = settings(tmp_path), account(tmp_path, balance=0.02)
    fake = FakeFal(IMAGE_RESULT)
    queue, _ = make_queue(fake, acct)
    with pytest.raises(BudgetExceededError, match="remaining account balance"):
        await flux(s, queue).generate_image(image_job(), tmp_path)
    assert fake.paths == []


def test_account_counts_reservations_and_settlements(tmp_path: Path) -> None:
    acct = account(tmp_path, balance=1.0)
    a = acct.reserve(endpoint="x", est_usd=0.6, ref="a")
    with pytest.raises(AccountLimitError):
        acct.reserve(endpoint="x", est_usd=0.5, ref="b")  # 0.6 reserved + 0.5 > 1.0
    acct.settle(a, 0.4)
    b = acct.reserve(endpoint="x", est_usd=0.5, ref="b")
    assert acct.summary().remaining_usd == pytest.approx(0.1)
    acct.release(b)
    assert acct.summary().remaining_usd == pytest.approx(0.6)
    # a second process sees the same wallet
    other = SpendAccount(tmp_path / "acct.db", balance_usd=1.0)
    assert other.summary().settled_usd == pytest.approx(0.4)


def test_stale_reservations_are_released_only_explicitly(tmp_path: Path) -> None:
    acct = account(tmp_path, balance=1.0)
    acct.reserve(endpoint="x", est_usd=0.3, ref="crashed")
    assert acct.release_stale(timedelta(hours=1)) == 0
    assert acct.release_stale(timedelta(seconds=-1)) == 1
    assert acct.summary().reserved_usd == 0


def _fal_runtime(tmp_path: Path, **over: Any) -> AgentRuntime:
    s = settings(
        tmp_path,
        media_provider="fal",
        fal_key="fal-test",
        fal_balance_usd=10.0,
        budget_usd=5.0,
        **over,
    )
    llm = ScriptedLLMProvider.from_scenario("perfume_en")
    return AgentRuntime.from_settings(s, llm_provider=llm)


def test_fal_run_budget_is_capped_by_the_remaining_wallet(tmp_path: Path) -> None:
    rt = _fal_runtime(tmp_path)
    orch = Orchestrator(rt)
    assert orch.create_run("perfume ad").budget.limit_usd == pytest.approx(5.0)
    sid = rt.fal_account().reserve(endpoint="x", est_usd=7.0, ref="earlier run")
    rt.fal_account().settle(sid, 7.0)
    run = orch.create_run("perfume ad")
    assert run.budget.limit_usd == pytest.approx(3.0)
    assert run.config.video_model == "kling_v3_pro" and run.config.image_model == "flux_dev"


def test_fal_requires_an_explicit_balance(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="FAL_BALANCE_USD"):
        settings(tmp_path, media_provider="fal", fal_key="k", fal_balance_usd=None)


def test_fal_gateway_uses_the_run_models(tmp_path: Path) -> None:
    rt = _fal_runtime(tmp_path, fal_video_model="ltx_23_fast", media_long_side=1280)
    run = Orchestrator(rt).create_run("perfume ad")
    gw = rt.media_gateway(run)
    assert gw.video.model == "fal-ai/ltx-2.3/image-to-video/fast"
    assert gw.image.model == FLUX and gw.long_side == 1280
    assert gw.adapter.name == "ltx"


async def test_locked_account_is_a_budget_failure_not_a_key_problem(tmp_path: Path) -> None:
    s, acct = settings(tmp_path), account(tmp_path)
    fake = FakeFal(IMAGE_RESULT)
    locked = {"detail": "User is locked. Reason: Exhausted balance. Top up your balance at x."}
    fake.faults["submit"] = [httpx.Response(403, json=locked)]  # verbatim shape seen live
    queue, _ = make_queue(fake, acct)
    with pytest.raises(BudgetExceededError, match="no usable balance"):
        await flux(s, queue).generate_image(image_job(), tmp_path)
    assert acct.summary().reserved_usd == 0


async def test_flux_2_flex_payload_has_only_its_schema_fields(tmp_path: Path) -> None:
    s, acct = settings(tmp_path), account(tmp_path)
    result = {"images": [{"url": f"{CDN}/kf.png", "width": 720, "height": 1280}], "seed": 0}
    fake = FakeFal(result, polls_in_progress=0)  # flex output has no has_nsfw_concepts
    queue, _ = make_queue(fake, acct)
    ep = s.catalog.fal.image.endpoints["flux_2_flex"]
    out = await FalImageProvider(queue, ep).generate_image(image_job(), tmp_path)
    body = fake.submitted[0]
    assert fake.paths[0] == "POST https://queue.fal.run/fal-ai/flux-2-flex"
    assert body["guidance_scale"] == 3.5 and body["num_inference_steps"] == 28
    assert "num_images" not in body and "negative_prompt" not in body
    assert out.cost_usd == pytest.approx(0.05)  # 0.92 MP rounds up to 1 MP at $0.05
    flux_dev = s.catalog.fal.image.endpoints["flux_dev"]
    assert flux_dev.params == {"num_inference_steps": 28, "num_images": 1}


# ----------------------------------------------------------------------------- crash-safe resume


async def test_a_new_process_resumes_the_persisted_request_instead_of_resubmitting(
    tmp_path: Path,
) -> None:
    import asyncio  # noqa: PLC0415

    from adsmith.domain.generation import RemoteRequest  # noqa: PLC0415
    from adsmith.providers.media.base import RemoteControl  # noqa: PLC0415

    s, acct = settings(tmp_path), account(tmp_path)
    fake = FakeFal(IMAGE_RESULT, polls_in_progress=10_000)  # the job is still running
    saved: list[RemoteRequest] = []
    submitted = asyncio.Event()

    def on_submit(req: RemoteRequest) -> None:
        saved.append(req)
        submitted.set()

    first, _ = make_queue(fake, acct)
    first._sleep = lambda _s: asyncio.sleep(0)  # yield so the 'crash' can land mid-poll
    first._timeout = 1e9
    job = image_job()
    crashed = asyncio.ensure_future(
        flux(s, first).generate_image(
            replace(job, remote=RemoteControl(on_submitted=on_submit)),
            tmp_path,
        )
    )
    await submitted.wait()  # persisted right after submit, before polling
    crashed.cancel()  # the process dies mid-poll
    with pytest.raises(asyncio.CancelledError):
        await crashed
    assert first.submits == 1 and acct.summary().reserved_usd == pytest.approx(0.025)

    fake.polls_in_progress = 0  # the job finished at fal meanwhile
    second, _ = make_queue(fake, acct)  # a fresh process: nothing in memory
    resumed = replace(job, remote=RemoteControl(resume=saved[0]))
    out = await flux(s, second).generate_image(resumed, tmp_path)
    assert second.submits == 0 and second.resumed == 1 and len(fake.submitted) == 1
    assert out.meta["request_id"] == saved[0].request_id
    summary = acct.summary()  # the dead process's reservation was settled, not duplicated
    assert summary.settled_usd == pytest.approx(0.025) and summary.reserved_usd == 0


async def test_a_resumed_request_that_is_gone_is_resubmitted_once(tmp_path: Path) -> None:
    from adsmith.domain.generation import RemoteRequest  # noqa: PLC0415
    from adsmith.providers.media.base import RemoteControl  # noqa: PLC0415

    s, acct = settings(tmp_path), account(tmp_path)
    stale_spend = acct.reserve(endpoint=FLUX, est_usd=0.025, ref="dead process")
    gone = RemoteRequest(
        request_id="old",
        status_url="https://queue.fal.run/x/requests/old/status",
        response_url="https://queue.fal.run/x/requests/old",
        spend_id=stale_spend,
    )
    fake = FakeFal(IMAGE_RESULT, polls_in_progress=0)
    fake.faults["status"] = [httpx.Response(404, json={"detail": "Request not found"})]
    queue, _ = make_queue(fake, acct)
    saved: list[RemoteRequest] = []
    job = replace(image_job(), remote=RemoteControl(resume=gone, on_submitted=saved.append))
    out = await flux(s, queue).generate_image(job, tmp_path)
    assert queue.resumed == 1 and queue.submits == 1 and saved[0].request_id == "req1"
    assert out.meta["request_id"] == "req1"
    summary = acct.summary()  # stale reservation released, the new one settled
    assert summary.reserved_usd == 0 and summary.settled_usd == pytest.approx(0.025)


async def test_kontext_payload_sends_the_reference_and_an_aspect_enum(tmp_path: Path) -> None:
    s, acct = settings(tmp_path), account(tmp_path)
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"\x89PNG reference")
    fake = FakeFal(IMAGE_RESULT)
    queue, _ = make_queue(fake, acct)
    provider = FalImageProvider(queue, s.catalog.fal.image.endpoints["kontext_pro"])
    job = replace(image_job(), reference_path=ref)
    out = await provider.generate_image(job, tmp_path)
    body = fake.submitted[0]
    assert fake.paths[0] == "POST https://queue.fal.run/fal-ai/flux-pro/kontext"
    assert body["image_url"].startswith("data:image/png;base64,")
    assert body["aspect_ratio"] == "9:16" and "image_size" not in body
    assert body["safety_tolerance"] == "2" and body["guidance_scale"] == 3.5
    assert out.cost_usd == pytest.approx(0.04)  # flat per image, whatever the size
    assert acct.summary().settled_usd == pytest.approx(0.04)


async def test_kontext_without_a_reference_is_refused_before_any_spend(tmp_path: Path) -> None:
    s, acct = settings(tmp_path), account(tmp_path)
    fake = FakeFal(IMAGE_RESULT)
    queue, _ = make_queue(fake, acct)
    provider = FalImageProvider(queue, s.catalog.fal.image.endpoints["kontext_pro"])
    with pytest.raises(PermanentProviderError):
        await provider.generate_image(image_job(), tmp_path)
    assert fake.submitted == [] and acct.summary().settled_usd == 0
