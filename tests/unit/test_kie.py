"""P1 (ADR-0027): the Kie.ai REST client and the Seedance 2.0 reference-to-video provider. No
network: ``httpx.MockTransport`` plays the documented API (docs.kie.ai, read 2026-11-07)."""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

from adsmith.agent.context import AgentRuntime, ad_endpoint, ad_model_key
from adsmith.agent.orchestrator import Orchestrator
from adsmith.agent.recovery.classifier import classify_kie
from adsmith.agent.recovery.errors import (
    AccountLockedError,
    BudgetExceededError,
    ContentPolicyError,
    PermanentProviderError,
    RateLimitedError,
    TransientProviderError,
)
from adsmith.config import Settings
from adsmith.domain.enums import ScenePhase
from adsmith.domain.generation import RemoteRequest
from adsmith.providers.llm.scripted import ScriptedLLMProvider
from adsmith.providers.media.base import (
    AMBIGUOUS_SUBMIT,
    MediaLabel,
    ReferenceVideoJob,
    RemoteControl,
    credit_quote,
)
from adsmith.providers.media.kie import (
    AmbiguousSubmitError,
    KieQueue,
    KieVideoProvider,
    TaskGoneError,
    result_url,
)
from adsmith.storage.account import SpendAccount

BASE = "https://api.kie.ai"
UP = "https://kieai.redpandaai.co"
MODEL = "bytedance/seedance-2-fast"  # the default since the standard price was refused
TMP = "https://tempfile.redpandaai.co/adsmith/photo.png"
CDN = "https://cdn.kie.test"
SECRET = "kie-secret-123"
PHOTO_URL = "https://tempfile.redpandaai.co/adsmith/ref.png"


def ok(data: Any) -> httpx.Response:
    return httpx.Response(200, json={"code": 200, "msg": "success", "data": data})


def err(code: int, msg: str) -> httpx.Response:
    """Kie's error shape: HTTP 200 with the error in the body's code."""
    return httpx.Response(200, json={"code": code, "msg": msg, "data": None})


class FakeKie:
    """Scriptable fake. ``faults`` maps a step to responses/exceptions consumed first."""

    def __init__(self, *, polls_running: int = 1, final: str = "success") -> None:
        self.polls_running, self.final = polls_running, final
        self.faults: dict[str, list[httpx.Response | Exception]] = {}
        self.submitted: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []
        self.credits = 80.0
        self.consumed: float | None = 70.2
        self._polls = 0
        self.accept_then_lose = 0  # the task starts, but the answer is lost

    def _fault(self, step: str) -> httpx.Response | None:
        queue = self.faults.get(step)
        if not queue:
            return None
        f = queue.pop(0)
        if isinstance(f, Exception):
            raise f
        return f

    def handler(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911
        self.requests.append(request)
        url = str(request.url)
        if url == f"{UP}/api/file-stream-upload":
            if (f := self._fault("upload")) is not None:
                return f
            return httpx.Response(
                200,
                json={
                    "success": True, "code": 200, "msg": "File uploaded successfully",
                    "data": {"fileName": "x.png", "downloadUrl": TMP, "mimeType": "image/png"},
                },
            )  # fmt: skip
        if url == f"{BASE}/api/v1/chat/credit":
            return self._fault("credits") or ok(self.credits)
        if url == f"{BASE}/api/v1/jobs/createTask":
            if (f := self._fault("submit")) is not None:
                return f
            self.submitted.append(json.loads(request.content))
            tid = f"task_{len(self.submitted)}"
            if self.accept_then_lose:
                self.accept_then_lose -= 1
                raise httpx.ReadTimeout("answer lost after the task was created")
            return ok({"taskId": tid})
        if url.startswith(f"{BASE}/api/v1/jobs/recordInfo"):
            if (f := self._fault("status")) is not None:
                return f
            tid = request.url.params["taskId"]
            self._polls += 1
            if self._polls <= self.polls_running:
                state = ("waiting", "queuing", "generating")[min(self._polls - 1, 2)]
                return ok({"taskId": tid, "state": state, "resultJson": "", "failCode": ""})
            rec: dict[str, Any] = {"taskId": tid, "model": MODEL, "state": self.final}
            if self.final == "success":
                rec["resultJson"] = json.dumps({"resultUrls": [f"{CDN}/{tid}.mp4"]})
                rec["creditsConsumed"] = self.consumed
            else:
                rec |= {"failCode": "500", "failMsg": "generation failed", "creditsConsumed": 0}
            return ok(rec)
        if url.startswith(CDN):
            return self._fault("download") or httpx.Response(200, content=b"fake mp4 bytes")
        return httpx.Response(404, json={"detail": f"no route {url}"})


def make_queue(
    fake: FakeKie, account: SpendAccount | None = None, *, timeout_s: float = 60.0
) -> tuple[KieQueue, list[float]]:
    sleeps: list[float] = []

    async def no_wait(s: float) -> None:
        sleeps.append(s)

    t = iter(range(100_000))
    queue = KieQueue(
        SECRET,
        base_url=BASE,
        upload_base_url=UP,
        client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        account=account,
        poll_interval_s=3.0,
        max_poll_interval_s=15.0,
        timeout_s=timeout_s,
        sleep=no_wait,
        clock=lambda: float(next(t)),
        rng=random.Random(1),
    )
    return queue, sleeps


def settings(tmp_path: Path, **over: Any) -> Settings:
    base: dict[str, Any] = {"data_dir": tmp_path / "data", "account_db": tmp_path / "acct.db"}
    return Settings(**(base | over))


async def _probe(_: Path) -> tuple[int, int, float]:
    return 480, 854, 6.04


def provider(tmp_path: Path, queue: KieQueue) -> KieVideoProvider:
    kie = settings(tmp_path).catalog.kie
    assert kie is not None
    return KieVideoProvider(queue, kie.video.endpoints["kie_seedance_20_fast"], _probe)


def job(**over: Any) -> ReferenceVideoJob:
    base = ReferenceVideoJob(
        prompt="Image 1 is the product. Shot 1 (0-2 s): ... Cut to: Shot 2 (2-4 s): ...",
        image_urls=(PHOTO_URL,),
        duration_s=6,
        resolution="480p",
        aspect_ratio="9:16",
        generate_audio=True,
        label=MediaLabel(scene_order=0, scene_version=1, phase=ScenePhase.CLIP, attempt_no=1),
        est_usd=0.351,
        computed_usd=0.351,
    )
    return replace(base, **over)


def account(tmp_path: Path, balance: float = 0.40) -> SpendAccount:
    return SpendAccount(tmp_path / "acct.db", balance_usd=balance, provider="kie")


# ----------------------------------------------------------------------------- catalog / price


def test_catalog_has_the_verified_kie_endpoint(tmp_path: Path) -> None:
    kie = settings(tmp_path).catalog.kie
    assert kie is not None and kie.base_url == BASE and kie.upload_base_url == UP
    ep = kie.video.endpoints[kie.video.default]
    assert ep.endpoint == MODEL and (ep.min_duration_s, ep.max_duration_s) == (4, 15)
    assert ep.resolutions == ["480p", "720p"]  # unpriced 1080p/4k are not offered
    assert ep.credit_pricing is not None and ep.credit_pricing.usd_per_credit == 0.005
    assert ep.max_images == 9 and "9:16" in ep.aspect_ratios


@pytest.mark.parametrize(
    ("res", "secs", "n_credits", "usd"),
    [("480p", 6, 70.2, 0.351), ("720p", 8, 198.4, 0.992), ("480p", 8, 93.6, 0.468)],
)
def test_credit_quote_per_resolution(
    tmp_path: Path, res: str, secs: int, n_credits: float, usd: float
) -> None:
    kie = settings(tmp_path).catalog.kie
    assert kie is not None
    cp = kie.video.endpoints["kie_seedance_20_fast"].credit_pricing
    assert cp is not None and cp.credits(res, secs) == pytest.approx(n_credits)
    q = credit_quote(cp, resolution=res, duration_s=secs)
    assert q.computed_usd == pytest.approx(usd) and q.reserved_usd == pytest.approx(usd)
    assert q.source == "credit_formula" and f"{n_credits:g} credits" in q.basis


async def test_quote_is_local_and_free(tmp_path: Path) -> None:
    fake = FakeKie()
    queue, _ = make_queue(fake)
    q = await provider(tmp_path, queue).quote(job())
    assert q.computed_usd == pytest.approx(0.351) and fake.requests == []


def test_keys_are_unique_across_providers_and_resolved_by_provider(tmp_path: Path) -> None:
    s = settings(tmp_path)
    assert ad_model_key(s, None, "kie") == "kie_seedance_20_fast"
    assert ad_model_key(s, None, "higgsfield") == "seedance_20_ref"
    assert ad_model_key(s, None, "mock") == "seedance_20_ref"
    assert ad_endpoint(s, "kie_seedance_20").credit_pricing is not None
    assert ad_endpoint(s, "seedance_20_ref").pricing is not None


def test_body_matches_the_schema(tmp_path: Path) -> None:
    queue, _ = make_queue(FakeKie())
    body = provider(tmp_path, queue).body(job())
    assert body == {
        "prompt": job().prompt,
        "reference_image_urls": [PHOTO_URL],
        "generate_audio": True,
        "resolution": "480p",
        "aspect_ratio": "9:16",
        "duration": 6,
    }
    assert "first_frame_url" not in body and "last_frame_url" not in body  # never mixed


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"duration_s": 16}, "duration 16s"),
        ({"resolution": "1080p"}, "not priced"),
        ({"aspect_ratio": "2:1"}, "aspect_ratio"),
        ({"image_urls": ()}, "reference images"),
        ({"image_urls": tuple(f"https://x/{i}.png" for i in range(10))}, "reference images"),
        ({"image_urls": ("asset://abc",)}, "public https"),
        ({"prompt": "x" * 2501}, "2500"),
    ],
)
async def test_invalid_jobs_are_refused_before_any_http(
    tmp_path: Path,
    over: dict[str, Any],
    message: str,
) -> None:
    fake = FakeKie()
    queue, _ = make_queue(fake, account(tmp_path))
    with pytest.raises(PermanentProviderError, match=message):
        await provider(tmp_path, queue).reference_to_video(job(**over), tmp_path)
    assert fake.requests == []


async def test_a_job_without_an_approved_estimate_is_never_submitted(tmp_path: Path) -> None:
    fake = FakeKie()
    queue, _ = make_queue(fake, account(tmp_path))
    with pytest.raises(PermanentProviderError, match="approved estimate"):
        await provider(tmp_path, queue).reference_to_video(job(est_usd=None), tmp_path)
    assert fake.requests == []


# ----------------------------------------------------------------------------- happy path


async def test_submit_poll_download_settle_at_consumed_credits(tmp_path: Path) -> None:
    acct = account(tmp_path)
    fake = FakeKie(polls_running=3)
    fake.consumed = 64.0  # Kie deducted less than our computed 70.2
    queue, sleeps = make_queue(fake, acct)
    out = await provider(tmp_path, queue).reference_to_video(job(), tmp_path)
    assert out.path.read_bytes() == b"fake mp4 bytes" and out.kind == "video"
    assert out.cost_usd == pytest.approx(0.32) and out.meta["credits_consumed"] == 64.0
    assert out.meta["request_id"] == "task_1" and queue.submits == 1
    check = out.meta["price_check"]
    assert check["credits_computed"] == pytest.approx(70.2) and check["credits_match"] is False
    submit = next(r for r in fake.requests if str(r.url).endswith("/createTask"))
    assert submit.headers["Authorization"] == f"Bearer {SECRET}"
    assert fake.submitted[0] == {"model": MODEL, "input": provider(tmp_path, queue).body(job())}
    assert acct.summary().settled_usd == pytest.approx(0.32) and acct.summary().reserved_usd == 0
    assert sleeps[:3] == pytest.approx([3.0, 4.5, 6.75], abs=1.7)  # backoff ×1.5 + jitter


async def test_no_consumed_credits_settles_at_the_computed_price(tmp_path: Path) -> None:
    acct = account(tmp_path)
    fake = FakeKie(polls_running=0)
    fake.consumed = None
    queue, _ = make_queue(fake, acct)
    out = await provider(tmp_path, queue).reference_to_video(job(), tmp_path)
    assert out.cost_usd == pytest.approx(0.351) and acct.summary().settled_usd == pytest.approx(
        0.351
    )


async def test_the_result_download_gets_no_api_key(tmp_path: Path) -> None:
    fake = FakeKie(polls_running=0)
    queue, _ = make_queue(fake, account(tmp_path))
    await provider(tmp_path, queue).reference_to_video(job(), tmp_path)
    cdn = next(r for r in fake.requests if str(r.url).startswith(CDN))
    assert "Authorization" not in cdn.headers


def test_result_url_parses_the_json_string_or_the_parsed_response() -> None:
    assert result_url({"resultJson": '{"resultUrls": ["https://a/b.mp4"]}'}) == "https://a/b.mp4"
    assert result_url({"response": {"resultUrls": ["https://c.mp4"]}}) == "https://c.mp4"
    assert result_url({"resultJson": ""}) is None and result_url({"resultJson": "{bad"}) is None


async def test_upload_is_multipart_on_the_upload_host_and_cached(tmp_path: Path) -> None:
    fake = FakeKie()
    queue, _ = make_queue(fake)
    img = tmp_path / "p.png"
    Image.new("RGB", (400, 400), (10, 20, 30)).save(img)
    assert await queue.upload(img) == TMP
    assert await queue.upload(img) == TMP  # cached by content hash
    ups = [r for r in fake.requests if str(r.url).startswith(UP)]
    assert len(ups) == 1 and queue.uploads == 1
    body = ups[0].content
    assert b'name="uploadPath"' in body and b"adsmith" in body and b'name="file"' in body
    assert ups[0].headers["Authorization"] == f"Bearer {SECRET}"


async def test_upload_refuses_an_unsupported_type(tmp_path: Path) -> None:
    fake = FakeKie()
    queue, _ = make_queue(fake)
    bad = tmp_path / "x.svg"
    bad.write_text("<svg/>")
    with pytest.raises(PermanentProviderError, match="allowed"):
        await queue.upload(bad)
    assert fake.requests == []


async def test_balance_is_a_free_number(tmp_path: Path) -> None:
    fake = FakeKie()
    queue, _ = make_queue(fake)
    assert await queue.credits() == 80.0


# ----------------------------------------------------------------------------- failures


def test_body_codes_are_classified() -> None:
    assert isinstance(classify_kie(402, "Insufficient credits"), AccountLockedError)
    assert isinstance(classify_kie(429, "slow down"), RateLimitedError)
    for code in (408, 455, 500):
        assert isinstance(classify_kie(code, "x"), TransientProviderError)
    assert isinstance(classify_kie(422, "duration invalid"), PermanentProviderError)
    assert isinstance(classify_kie(501, "generation failed"), PermanentProviderError)
    assert isinstance(classify_kie(400, "content flagged as sensitive"), ContentPolicyError)
    bad_key = classify_kie(401, "unauthorized")
    assert isinstance(bad_key, PermanentProviderError) and "KIE_API_KEY" in bad_key.message


async def test_http_200_with_error_code_402_pauses_all_paid_work(tmp_path: Path) -> None:
    acct = account(tmp_path)
    fake = FakeKie()
    fake.faults["submit"] = [err(402, "Insufficient credits")]
    queue, _ = make_queue(fake, acct)
    p = provider(tmp_path, queue)
    with pytest.raises(AccountLockedError):
        await p.reference_to_video(job(), tmp_path)
    assert acct.summary().reserved_usd == 0  # released: nothing started
    with pytest.raises(AccountLockedError, match="paused"):
        await p.reference_to_video(job(), tmp_path)
    assert len(fake.submitted) == 0


async def test_validation_error_on_submit_is_permanent_and_released(tmp_path: Path) -> None:
    acct = account(tmp_path)
    fake = FakeKie()
    fake.faults["submit"] = [err(422, "aspect_ratio invalid")]
    queue, _ = make_queue(fake, acct)
    with pytest.raises(PermanentProviderError, match="422"):
        await provider(tmp_path, queue).reference_to_video(job(), tmp_path)
    assert acct.summary().reserved_usd == 0


async def test_a_failed_task_is_not_charged_and_released(tmp_path: Path) -> None:
    acct = account(tmp_path)
    fake = FakeKie(polls_running=0, final="fail")
    queue, _ = make_queue(fake, acct)
    with pytest.raises(PermanentProviderError, match="not charged"):
        await provider(tmp_path, queue).reference_to_video(job(), tmp_path)
    assert acct.summary().reserved_usd == 0 and acct.summary().settled_usd == 0


async def test_a_moderated_task_is_content_policy(tmp_path: Path) -> None:
    acct = account(tmp_path)
    fake = FakeKie(polls_running=0)
    fake.faults["status"] = [
        ok({"taskId": "task_1", "state": "fail", "failCode": "400", "failMsg": "NSFW content"})
    ]
    queue, _ = make_queue(fake, acct)
    with pytest.raises(ContentPolicyError):
        await provider(tmp_path, queue).reference_to_video(job(), tmp_path)
    assert acct.summary().reserved_usd == 0


async def test_rate_limit_and_maintenance_while_polling_keep_the_task(tmp_path: Path) -> None:
    acct = account(tmp_path)
    fake = FakeKie(polls_running=0)
    fake.faults["status"] = [err(429, "rate limited"), err(455, "maintenance")]
    queue, _ = make_queue(fake, acct)
    p = provider(tmp_path, queue)
    with pytest.raises(RateLimitedError):
        await p.reference_to_video(job(), tmp_path)
    with pytest.raises(TransientProviderError):
        await p.reference_to_video(job(), tmp_path)
    out = await p.reference_to_video(job(), tmp_path)  # the recovery layer's retry: polls
    assert len(fake.submitted) == 1 and queue.submits == 1 and out.meta["request_id"] == "task_1"
    assert acct.summary().reserved_usd == 0


async def test_poll_deadline_keeps_the_task_for_resumption(tmp_path: Path) -> None:
    acct = account(tmp_path)
    fake = FakeKie(polls_running=10_000)
    queue, _ = make_queue(fake, acct, timeout_s=5)
    with pytest.raises(TransientProviderError, match="kept"):
        await provider(tmp_path, queue).reference_to_video(job(), tmp_path)
    assert acct.summary().reserved_usd == pytest.approx(0.351)  # still rendering: still held


async def test_a_connect_error_on_submit_is_released_and_transient(tmp_path: Path) -> None:
    acct = account(tmp_path)
    fake = FakeKie()
    fake.faults["submit"] = [httpx.ConnectError("no route")]
    queue, _ = make_queue(fake, acct)
    with pytest.raises(TransientProviderError):
        await provider(tmp_path, queue).reference_to_video(job(), tmp_path)
    assert acct.summary().reserved_usd == 0 and queue.paused is None


async def test_an_ambiguous_submit_keeps_the_reservation_and_never_resubmits(
    tmp_path: Path,
) -> None:
    acct = account(tmp_path)
    fake = FakeKie(polls_running=0)
    fake.accept_then_lose = 1  # Kie created task_1, the answer was lost
    queue, _ = make_queue(fake, acct)
    p = provider(tmp_path, queue)
    with pytest.raises(AmbiguousSubmitError) as info:
        await p.reference_to_video(job(), tmp_path)
    assert info.value.message.startswith(AMBIGUOUS_SUBMIT)
    assert acct.summary().reserved_usd == pytest.approx(0.351)  # fail-safe: kept
    with pytest.raises(AccountLockedError, match="paused"):  # no second paid submit
        await p.reference_to_video(job(), tmp_path)
    assert len(fake.submitted) == 1


async def test_wallet_ceiling_refuses_before_any_http(tmp_path: Path) -> None:
    fake = FakeKie()
    queue, _ = make_queue(fake, account(tmp_path, balance=0.30))
    with pytest.raises(BudgetExceededError, match="remaining account balance"):
        await provider(tmp_path, queue).reference_to_video(job(), tmp_path)
    assert fake.requests == []


async def test_a_new_process_resumes_the_persisted_task(tmp_path: Path) -> None:
    acct = account(tmp_path)
    fake = FakeKie(polls_running=10_000)
    saved: list[RemoteRequest] = []
    submitted = asyncio.Event()

    def on_submit(req: RemoteRequest) -> None:
        saved.append(req)
        submitted.set()

    first, _ = make_queue(fake, acct)
    first._sleep = lambda _s: asyncio.sleep(0)  # yield so the 'crash' lands mid-poll
    first._timeout = 1e9
    task = asyncio.ensure_future(
        provider(tmp_path, first).reference_to_video(
            job(remote=RemoteControl(on_submitted=on_submit)), tmp_path
        )
    )
    await submitted.wait()  # persisted right after submit, before polling
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert acct.summary().reserved_usd == pytest.approx(0.351)
    assert saved[0].request_id == "task_1" and "taskId=task_1" in saved[0].status_url

    fake.polls_running = 0
    second, _ = make_queue(fake, acct)  # nothing in memory
    out = await provider(tmp_path, second).reference_to_video(
        job(remote=RemoteControl(resume=saved[0])), tmp_path
    )
    assert second.submits == 0 and second.resumed == 1 and len(fake.submitted) == 1
    assert out.meta["request_id"] == "task_1"
    assert acct.summary().reserved_usd == 0


async def test_a_resumed_task_unknown_to_kie_is_not_resubmitted(tmp_path: Path) -> None:
    acct = account(tmp_path)
    stale = acct.reserve(endpoint=MODEL, est_usd=0.351, ref="dead process")
    gone = RemoteRequest(
        request_id="old",
        status_url=f"{BASE}/api/v1/jobs/recordInfo?taskId=old",
        response_url=f"{BASE}/api/v1/jobs/recordInfo?taskId=old",
        spend_id=stale,
    )
    fake = FakeKie(polls_running=0)
    fake.faults["status"] = [err(422, "recordInfo is null")]
    queue, _ = make_queue(fake, acct)
    with pytest.raises(TaskGoneError):
        await provider(tmp_path, queue).reference_to_video(
            job(remote=RemoteControl(resume=gone)), tmp_path
        )
    assert queue.submits == 0 and acct.summary().reserved_usd == 0


async def test_the_key_never_appears_in_repr_or_errors(tmp_path: Path) -> None:
    fake = FakeKie()
    fake.faults["submit"] = [err(401, "unauthorized")]
    queue, _ = make_queue(fake, account(tmp_path))
    assert SECRET not in repr(queue)
    with pytest.raises(PermanentProviderError) as info:
        await provider(tmp_path, queue).reference_to_video(job(), tmp_path)
    assert SECRET not in info.value.message


# ----------------------------------------------------------------------------- runtime wiring


def _kie_runtime(tmp_path: Path, **over: Any) -> AgentRuntime:
    s = settings(tmp_path, ad_provider="kie", kie_api_key=SECRET, kie_balance_usd=0.40, **over)
    return AgentRuntime.from_settings(
        s, llm_provider=ScriptedLLMProvider.from_scenario("perfume_en")
    )


def test_kie_requires_a_key_and_a_ceiling(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="KIE_API_KEY"):
        settings(tmp_path, ad_provider="kie", kie_api_key=None, kie_balance_usd=0.4)
    with pytest.raises(ValueError, match="KIE_BALANCE_USD"):
        settings(tmp_path, ad_provider="kie", kie_api_key=SECRET)


def test_runtime_builds_the_provider_and_caps_media_by_the_kie_wallet(tmp_path: Path) -> None:
    rt = _kie_runtime(tmp_path, kie_resolution="480p")
    assert rt.kie_video().model == MODEL
    run = Orchestrator(rt).create_run("perfume ad")
    assert run.config.ad_provider == "kie" and run.config.ad_model == "kie_seedance_20_fast"
    assert run.config.ad_resolution == "480p"
    assert run.budget.media_limit_usd == pytest.approx(0.40)
    sid = rt.kie_account().reserve(endpoint=MODEL, est_usd=0.35, ref="earlier")
    rt.kie_account().settle(sid, 0.35)
    assert Orchestrator(rt).create_run("perfume ad").budget.media_limit_usd == pytest.approx(0.05)
    assert SpendAccount(tmp_path / "acct.db", balance_usd=1.0, provider="higgsfield").summary(
    ).settled_usd == 0  # fmt: skip


def test_a_real_kie_ad_gets_the_vision_critic(tmp_path: Path) -> None:
    s = settings(tmp_path, ad_provider="kie", kie_api_key=SECRET, kie_balance_usd=0.4)
    assert s.media_provider == "mock" and s.resolved_qa_mode() == "llm"


async def test_the_standard_models_unverified_price_is_never_quoted(tmp_path: Path) -> None:
    kie = settings(tmp_path).catalog.kie
    assert kie is not None
    std = kie.video.endpoints["kie_seedance_20"]
    assert std.endpoint == "bytedance/seedance-2" and std.unverified_price
    fake = FakeKie()
    queue, _ = make_queue(fake)
    with pytest.raises(PermanentProviderError, match="price unverified"):
        await KieVideoProvider(queue, std, _probe).quote(job())
    assert fake.requests == []
