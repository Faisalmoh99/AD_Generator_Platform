"""Phase A: the Higgsfield REST client and the Seedance reference-to-video provider. No network:
``httpx.MockTransport`` plays the documented API (docs.higgsfield.ai, read 2026-11-06)."""

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

from adsmith.agent.context import AgentRuntime
from adsmith.agent.orchestrator import Orchestrator
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
from adsmith.providers.media.base import MediaLabel, ReferenceVideoJob, RemoteControl
from adsmith.providers.media.higgsfield import (
    HiggsfieldQueue,
    HiggsfieldVideoProvider,
    idempotency_key,
)
from adsmith.storage.account import SpendAccount

BASE = "https://api.higgsfield.ai"
EP = "bytedance/seedance-2.0/reference-to-video"
CDN = "https://cdn.higgsfield.test"
UPLOAD = "https://uploads.s3.test/put"
SECRET = "kid-123:ksecret-456"
PHOTO_URL = "https://public.higgsfield.test/photo.png"


class FakeHiggsfield:
    """Scriptable fake. ``faults`` maps a step to responses/exceptions consumed first."""

    def __init__(self, *, polls_in_progress: int = 1, final: str = "completed") -> None:
        self.polls_in_progress, self.final = polls_in_progress, final
        self.faults: dict[str, list[httpx.Response | Exception]] = {}
        self.submitted: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []
        self.idempotency: list[str] = []
        self.estimate_usd = "1.80"
        self._polls = 0
        self.by_key: dict[str, str] = {}
        self.accept_then_timeout = 0  # the server queues the job, but the response is lost

    def _fault(self, step: str) -> httpx.Response | None:
        queue = self.faults.get(step)
        if not queue:
            return None
        f = queue.pop(0)
        if isinstance(f, Exception):
            raise f
        return f

    def handler(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911, PLR0912
        self.requests.append(request)
        url = str(request.url)
        if url == f"{BASE}/files/generate-upload-url":
            if (f := self._fault("upload")) is not None:
                return f
            return httpx.Response(
                200,
                json={
                    "public_url": PHOTO_URL,
                    "upload_url": UPLOAD,
                    "content_type": json.loads(request.content)["content_type"],
                    "upload_headers": {"x-amz-tagging": "retention=temporary"},
                },
            )
        if url == UPLOAD:
            return self._fault("put") or httpx.Response(200)
        if url.startswith(f"{BASE}/estimate/"):
            if (f := self._fault("estimate")) is not None:
                return f
            return httpx.Response(200, json={"usd": self.estimate_usd})
        if request.method == "POST" and url == f"{BASE}/{EP}":
            if (f := self._fault("submit")) is not None:
                return f
            key = request.headers.get("Idempotency-Key", "")
            self.idempotency.append(key)
            if key in self.by_key:  # the server returns the existing request for a reused key
                rid = self.by_key[key]
            else:
                self.submitted.append(json.loads(request.content))
                rid = f"req{len(self.submitted)}"
                self.by_key[key] = rid
            if self.accept_then_timeout:
                self.accept_then_timeout -= 1
                raise httpx.ReadTimeout("response lost after the job was queued")
            return httpx.Response(
                200,
                json={
                    "status": "queued",
                    "request_id": rid,
                    "status_url": f"{BASE}/requests/{rid}/status",
                    "cancel_url": f"{BASE}/requests/{rid}/cancel",
                },
            )
        if url.endswith("/status"):
            if (f := self._fault("status")) is not None:
                return f
            self._polls += 1
            rid = url.split("/requests/")[1].split("/", maxsplit=1)[0]
            if self._polls <= self.polls_in_progress:
                return httpx.Response(200, json={"status": "in_progress", "request_id": rid})
            body: dict[str, Any] = {"status": self.final, "request_id": rid, "error": None}
            if self.final == "completed":
                body["video"] = {"url": f"{CDN}/{rid}.mp4"}
                body["expires_at"] = "2026-11-13T00:00:00Z"
            if self.final == "failed":
                body["error"] = "generation failed upstream"
            return httpx.Response(200, json=body)
        if url.startswith(CDN):
            return self._fault("download") or httpx.Response(200, content=b"fake mp4 bytes")
        return httpx.Response(404, json={"detail": f"no route {url}"})


def make_queue(
    fake: FakeHiggsfield, account: SpendAccount | None = None, *, timeout_s: float = 60.0
) -> tuple[HiggsfieldQueue, list[float]]:
    sleeps: list[float] = []

    async def no_wait(s: float) -> None:
        sleeps.append(s)

    t = iter(range(100_000))
    queue = HiggsfieldQueue(
        SECRET,
        base_url=BASE,
        client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        account=account,
        poll_interval_s=2.0,
        max_poll_interval_s=10.0,
        timeout_s=timeout_s,
        sleep=no_wait,
        clock=lambda: float(next(t)),
        rng=random.Random(1),
    )
    return queue, sleeps


def settings(tmp_path: Path, **over: Any) -> Settings:
    base: dict[str, Any] = {"data_dir": tmp_path / "data", "account_db": tmp_path / "acct.db"}
    return Settings(**(base | over))


def provider(
    tmp_path: Path, queue: HiggsfieldQueue, key: str = "seedance_20_ref"
) -> HiggsfieldVideoProvider:
    catalog = settings(tmp_path).catalog.higgsfield
    assert catalog is not None

    async def probe(_: Path) -> tuple[int, int, float]:
        return 720, 1280, 12.04

    return HiggsfieldVideoProvider(queue, catalog.video.endpoints[key], probe)


def job(**over: Any) -> ReferenceVideoJob:
    base = ReferenceVideoJob(
        prompt="Shot 1 (0-3 s): macro of the cap. Shot 2 (3-7 s): ...",
        image_urls=(PHOTO_URL,),
        duration_s=12,
        resolution="720p",
        aspect_ratio="9:16",
        generate_audio=True,
        label=MediaLabel(scene_order=0, scene_version=1, phase=ScenePhase.CLIP, attempt_no=1),
        est_usd=1.80,
    )
    return replace(base, **over)


def account(tmp_path: Path, balance: float = 5.0) -> SpendAccount:
    return SpendAccount(tmp_path / "acct.db", balance_usd=balance, provider="higgsfield")


# ----------------------------------------------------------------------------- catalog / body


def test_catalog_has_both_seedance_endpoints_with_the_verified_schema(tmp_path: Path) -> None:
    hf = settings(tmp_path).catalog.higgsfield
    assert hf is not None and hf.base_url == BASE and hf.video.default == "seedance_20_ref"
    v20, v25 = hf.video.endpoints["seedance_20_ref"], hf.video.endpoints["seedance_25_ref"]
    assert v20.endpoint == EP and (v20.min_duration_s, v20.max_duration_s) == (4, 15)
    assert "4k" in v20.resolutions and "4k" not in v25.resolutions
    assert v25.max_duration_s == 30 and v20.max_images == 9 and v25.max_images == 30
    assert "9:16" in v20.aspect_ratios


def test_body_matches_the_schema(tmp_path: Path) -> None:
    queue, _ = make_queue(FakeHiggsfield())
    body = provider(tmp_path, queue).body(job())
    assert body == {
        "prompt": job().prompt,
        "image_urls": [PHOTO_URL],
        "duration": 12,
        "resolution": "720p",
        "aspect_ratio": "9:16",
        "generate_audio": True,
    }
    assert "image_url" not in body and "end_image_url" not in body  # never mixed


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"duration_s": 16}, "duration 16s"),
        ({"resolution": "2k"}, "resolution"),
        ({"aspect_ratio": "2:1"}, "aspect_ratio"),
        ({"image_urls": ()}, "reference images"),
        ({"image_urls": ("asset://abc",)}, "public https"),
    ],
)
async def test_invalid_jobs_are_refused_before_any_http(
    tmp_path: Path, over: dict[str, Any], message: str
) -> None:
    fake = FakeHiggsfield()
    queue, _ = make_queue(fake, account(tmp_path))
    with pytest.raises(PermanentProviderError, match=message):
        await provider(tmp_path, queue).reference_to_video(job(**over), tmp_path)
    assert fake.requests == []


async def test_a_job_without_an_approved_estimate_is_never_submitted(tmp_path: Path) -> None:
    fake = FakeHiggsfield()
    queue, _ = make_queue(fake, account(tmp_path))
    with pytest.raises(PermanentProviderError, match="approved estimate"):
        await provider(tmp_path, queue).reference_to_video(job(est_usd=None), tmp_path)
    assert fake.requests == []


# ----------------------------------------------------------------------------- happy path


async def test_submit_poll_download_settle(tmp_path: Path) -> None:
    acct = account(tmp_path)
    fake = FakeHiggsfield(polls_in_progress=3)
    queue, sleeps = make_queue(fake, acct)
    out = await provider(tmp_path, queue).reference_to_video(job(), tmp_path)
    assert out.path.read_bytes() == b"fake mp4 bytes" and out.kind == "video"
    assert out.cost_usd == pytest.approx(1.80) and out.duration_s == 12.04
    assert out.meta["request_id"] == "req1" and queue.submits == 1
    submit = next(r for r in fake.requests if r.method == "POST" and str(r.url).endswith(EP))
    assert submit.headers["Authorization"] == f"Key {SECRET}"
    assert submit.headers["Idempotency-Key"] == idempotency_key(EP, fake.submitted[0])
    # documented polling: 2 s, growing ×1.5 to at most 10 s, with up to 25 % jitter
    assert len(sleeps) == 3 and 2.0 <= sleeps[0] <= 2.5 and 3.0 <= sleeps[1] <= 3.75
    summary = acct.summary()
    assert summary.settled_usd == pytest.approx(1.80) and summary.reserved_usd == 0


async def test_cdn_download_gets_no_api_key(tmp_path: Path) -> None:
    fake = FakeHiggsfield(polls_in_progress=0)
    queue, _ = make_queue(fake)
    await provider(tmp_path, queue).reference_to_video(job(), tmp_path)
    cdn = next(r for r in fake.requests if str(r.url).startswith(CDN))
    assert "Authorization" not in cdn.headers


async def test_variants_have_distinct_idempotency_keys(tmp_path: Path) -> None:
    fake = FakeHiggsfield(polls_in_progress=0)
    queue, _ = make_queue(fake)
    p = provider(tmp_path, queue)
    await p.reference_to_video(job(), tmp_path)
    await p.reference_to_video(job(variant=1), tmp_path)
    assert len(set(fake.idempotency)) == 2 and queue.submits == 2


# ----------------------------------------------------------------------------- estimate / upload


async def test_estimate_is_parsed_and_reserves_nothing(tmp_path: Path) -> None:
    acct = account(tmp_path)
    fake = FakeHiggsfield()
    queue, _ = make_queue(fake, acct)
    q = await provider(tmp_path, queue).quote(job(est_usd=None))
    assert q.source == "estimate_api" and q.computed_usd == q.reserved_usd == pytest.approx(1.80)
    assert queue.estimates == 1 and queue.submits == 0
    est = fake.requests[0]
    assert str(est.url) == f"{BASE}/estimate/{EP}"
    assert json.loads(est.content)["duration"] == 12
    assert acct.summary().reserved_usd == 0 and acct.summary().settled_usd == 0


async def test_an_estimate_above_the_sanity_cap_is_refused(tmp_path: Path) -> None:
    fake = FakeHiggsfield()
    fake.estimate_usd = "18.00"  # 12 s × $1/s cap = $12
    queue, _ = make_queue(fake)
    with pytest.raises(BudgetExceededError, match="sanity cap"):
        await provider(tmp_path, queue).quote(job(est_usd=None))


# The exact body the live /estimate returned for Seedance 2.0 on 2026-11-06 (no "usd" key).
LIVE_DESCRIPTION = {
    "type": "description",
    "pricing_description": (
        "Token-metered pricing. Billable video tokens = ceil((input video seconds + generated "
        "video seconds) × output width × output height × 24 fps / 1024). Image and audio "
        "references do not count as video input. Per 1,000 video tokens: without video input — "
        "480p/720p/1080p $0.014, 4K $0.008; with video input — 480p/720p/1080p $0.0084, 4K "
        "$0.0048 (0.6× the standard rate). With video references, both input and generated video "
        "durations are billable. Rates shown are before any applicable customer discount."
    ),
}


def describe(fake: FakeHiggsfield, body: dict[str, Any] = LIVE_DESCRIPTION) -> None:
    fake.faults["estimate"] = [httpx.Response(200, json=body)]


async def test_a_pricing_description_is_priced_by_the_token_formula_plus_margin(
    tmp_path: Path,
) -> None:
    acct = account(tmp_path)
    fake = FakeHiggsfield()
    describe(fake)
    queue, _ = make_queue(fake, acct)
    q = await provider(tmp_path, queue).quote(job(est_usd=None, duration_s=8))
    # 720x1280 x 8 s x 24 fps / 1024 = 172,800 tokens x $0.014 / 1000 = $2.4192
    assert q.source == "token_formula" and (q.width, q.height) == (720, 1280)
    assert q.computed_usd == pytest.approx(2.4192)
    assert q.reserved_usd == pytest.approx(2.4192 * 1.10)
    assert "172,800 tokens" in q.basis and "+10% margin" in q.basis
    assert acct.summary().reserved_usd == 0 and queue.submits == 0


@pytest.mark.parametrize(
    ("resolution", "aspect", "seconds", "size", "usd"),
    [
        ("720p", "9:16", 12, (720, 1280), 3.6288),
        ("1080p", "9:16", 12, (1080, 1920), 8.1648),
        ("480p", "9:16", 8, (480, 854), 1.07604),
        ("720p", "16:9", 8, (1280, 720), 2.4192),
        ("1080p", "16:9", 8, (1920, 1080), 5.4432),
    ],
)
async def test_token_formula_per_resolution(
    tmp_path: Path,
    resolution: str,
    aspect: str,
    seconds: int,
    size: tuple[int, int],
    usd: float,
) -> None:
    fake = FakeHiggsfield()
    describe(fake)
    queue, _ = make_queue(fake)
    q = await provider(tmp_path, queue).quote(
        job(est_usd=None, resolution=resolution, aspect_ratio=aspect, duration_s=seconds)
    )
    assert (q.width, q.height) == size and q.computed_usd == pytest.approx(usd, abs=1e-3)


async def test_a_description_that_no_longer_names_our_rates_is_refused(tmp_path: Path) -> None:
    fake = FakeHiggsfield()
    changed = LIVE_DESCRIPTION["pricing_description"].replace("$0.014", "$0.020")
    describe(fake, {"type": "description", "pricing_description": changed})
    queue, _ = make_queue(fake)
    with pytest.raises(PermanentProviderError, match=r"no longer names \['\$0.014'\]"):
        await provider(tmp_path, queue).quote(job(est_usd=None))


async def test_a_description_without_configured_pricing_is_refused(tmp_path: Path) -> None:
    fake = FakeHiggsfield()
    describe(fake)
    queue, _ = make_queue(fake)
    with pytest.raises(PermanentProviderError, match="no token pricing is configured"):
        await provider(tmp_path, queue, "seedance_25_ref").quote(job(est_usd=None))


async def test_settles_at_the_price_and_logs_the_real_size_next_to_it(tmp_path: Path) -> None:
    acct = account(tmp_path)
    fake = FakeHiggsfield()
    queue, _ = make_queue(fake, acct)
    out = await provider(tmp_path, queue).reference_to_video(
        job(est_usd=2.66112, computed_usd=2.4192, duration_s=8), tmp_path
    )
    assert out.cost_usd == pytest.approx(2.4192)
    s = acct.summary()
    assert s.settled_usd == pytest.approx(2.4192, abs=1e-3) and s.reserved_usd == 0
    check = out.meta["price_check"]
    assert check["assumed_size"] == "720x1280" and check["actual_size"] == "720x1280"
    assert check["size_matches"] is True and check["computed_usd"] == pytest.approx(2.4192)
    assert check["reserved_usd"] == pytest.approx(2.66112)


async def test_upload_uses_the_presigned_url_without_the_key_and_is_cached(tmp_path: Path) -> None:
    img = tmp_path / "photo.png"
    Image.new("RGB", (8, 8), (200, 120, 30)).save(img)
    fake = FakeHiggsfield()
    queue, _ = make_queue(fake)
    assert await queue.upload(img) == PHOTO_URL
    assert await queue.upload(img) == PHOTO_URL  # cached: one upload only
    assert queue.uploads == 1
    slot = fake.requests[0]
    assert json.loads(slot.content) == {"content_type": "image/png"}
    assert slot.headers["Authorization"] == f"Key {SECRET}"
    put = fake.requests[1]
    assert put.method == "PUT" and str(put.url) == UPLOAD
    assert "Authorization" not in put.headers
    assert put.headers["x-amz-tagging"] == "retention=temporary"
    assert put.headers["Content-Type"] == "image/png"


async def test_upload_refuses_an_unsupported_type(tmp_path: Path) -> None:
    doc = tmp_path / "brief.pdf"
    doc.write_bytes(b"%PDF")
    fake = FakeHiggsfield()
    queue, _ = make_queue(fake)
    with pytest.raises(PermanentProviderError, match="allowed"):
        await queue.upload(doc)
    assert fake.requests == []


# ----------------------------------------------------------------------------- terminal states


@pytest.mark.parametrize(
    ("final", "error"),
    [("failed", PermanentProviderError), ("canceled", PermanentProviderError),
     ("nsfw", ContentPolicyError)],
)  # fmt: skip
async def test_uncharged_terminal_states_release_the_reservation(
    tmp_path: Path, final: str, error: type[Exception]
) -> None:
    acct = account(tmp_path)
    queue, _ = make_queue(FakeHiggsfield(polls_in_progress=0, final=final), acct)
    with pytest.raises(error, match="not charged"):
        await provider(tmp_path, queue).reference_to_video(job(), tmp_path)
    assert acct.summary().reserved_usd == 0 and acct.summary().settled_usd == 0


async def test_completed_without_a_video_is_permanent_and_released(tmp_path: Path) -> None:
    acct = account(tmp_path)
    fake = FakeHiggsfield(polls_in_progress=0)
    fake.faults["status"] = [httpx.Response(200, json={"status": "completed", "video": None})]
    queue, _ = make_queue(fake, acct)
    with pytest.raises(PermanentProviderError, match="no video"):
        await provider(tmp_path, queue).reference_to_video(job(), tmp_path)
    assert acct.summary().reserved_usd == 0


# ----------------------------------------------------------------------------- error mapping


async def test_full_account_queue_is_rate_limited_with_retry_after(tmp_path: Path) -> None:
    acct = account(tmp_path)
    fake = FakeHiggsfield()
    body = {"code": "account_queue_full", "retryable": True, "limit": 40}
    fake.faults["submit"] = [httpx.Response(429, json=body, headers={"Retry-After": "12"})]
    queue, _ = make_queue(fake, acct)
    with pytest.raises(RateLimitedError) as info:
        await provider(tmp_path, queue).reference_to_video(job(), tmp_path)
    assert info.value.retry_after_s == 12.0 and acct.summary().reserved_usd == 0


async def test_admission_control_is_transient(tmp_path: Path) -> None:
    fake = FakeHiggsfield()
    fake.faults["submit"] = [
        httpx.Response(503, json={"code": "admission_control_unavailable", "retryable": True})
    ]
    queue, _ = make_queue(fake)
    with pytest.raises(TransientProviderError):
        await provider(tmp_path, queue).reference_to_video(job(), tmp_path)


async def test_no_credits_pauses_all_paid_work(tmp_path: Path) -> None:
    acct = account(tmp_path)
    fake = FakeHiggsfield()
    fake.faults["submit"] = [httpx.Response(402, json={"detail": "Insufficient credits"})]
    queue, _ = make_queue(fake, acct)
    p = provider(tmp_path, queue)
    with pytest.raises(AccountLockedError):
        await p.reference_to_video(job(), tmp_path)
    with pytest.raises(AccountLockedError, match="paused"):
        await p.reference_to_video(job(variant=1), tmp_path)
    assert len(fake.submitted) == 0 and acct.summary().reserved_usd == 0


async def test_bad_key_is_permanent_and_names_the_env_var_not_the_value(tmp_path: Path) -> None:
    fake = FakeHiggsfield()
    fake.faults["submit"] = [httpx.Response(401, json={"detail": "invalid credentials"})]
    queue, _ = make_queue(fake)
    with pytest.raises(PermanentProviderError) as info:
        await provider(tmp_path, queue).reference_to_video(job(), tmp_path)
    assert "HF_KEY" in str(info.value) and SECRET not in str(info.value)
    assert "ksecret" not in repr(queue)


async def test_policy_rejection_on_submit_is_content_policy(tmp_path: Path) -> None:
    fake = FakeHiggsfield()
    fake.faults["submit"] = [httpx.Response(422, json={"detail": "prompt flagged by moderation"})]
    queue, _ = make_queue(fake)
    with pytest.raises(ContentPolicyError):
        await provider(tmp_path, queue).reference_to_video(job(), tmp_path)


# ----------------------------------------------------------------------------- no double spend


@pytest.mark.parametrize("step", ["status", "download"])
async def test_transient_after_submit_resumes_instead_of_resubmitting(
    tmp_path: Path, step: str
) -> None:
    acct = account(tmp_path)
    fake = FakeHiggsfield(polls_in_progress=0)
    fake.faults[step] = [httpx.Response(503, text="upstream unavailable")]
    queue, _ = make_queue(fake, acct)
    p = provider(tmp_path, queue)
    with pytest.raises(TransientProviderError):
        await p.reference_to_video(job(), tmp_path)
    assert acct.summary().reserved_usd == pytest.approx(1.80)  # still in flight
    out = await p.reference_to_video(job(), tmp_path)  # the recovery layer's retry
    assert queue.submits == 1 and len(fake.submitted) == 1 and out.meta["request_id"] == "req1"
    assert acct.summary().settled_usd == pytest.approx(1.80) and acct.summary().reserved_usd == 0


async def test_poll_deadline_keeps_the_request_for_resumption(tmp_path: Path) -> None:
    fake = FakeHiggsfield(polls_in_progress=10_000)
    queue, _ = make_queue(fake, timeout_s=5.0)
    p = provider(tmp_path, queue)
    with pytest.raises(TransientProviderError, match="kept"):
        await p.reference_to_video(job(), tmp_path)
    fake.polls_in_progress = 0
    await p.reference_to_video(job(), tmp_path)
    assert queue.submits == 1


async def test_ambiguous_submit_timeout_keeps_the_reservation_and_reuses_the_key(
    tmp_path: Path,
) -> None:
    acct = account(tmp_path)
    fake = FakeHiggsfield(polls_in_progress=0)
    fake.accept_then_timeout = 1  # Higgsfield queued (and would bill) the job; we never heard
    queue, _ = make_queue(fake, acct)
    p = provider(tmp_path, queue)
    with pytest.raises(TransientProviderError, match="reservation kept"):
        await p.reference_to_video(job(), tmp_path)
    assert acct.summary().reserved_usd == pytest.approx(1.80)
    out = await p.reference_to_video(job(), tmp_path)
    # the retry carried the same key, so the server answered with the queued job
    assert len(fake.submitted) == 1 and out.meta["request_id"] == "req1"
    assert fake.idempotency[0] == fake.idempotency[1]
    summary = acct.summary()  # one job, one reservation: settled once, nothing left held
    assert summary.settled_usd == pytest.approx(1.80) and summary.reserved_usd == 0


async def test_wallet_ceiling_refuses_before_any_http(tmp_path: Path) -> None:
    fake = FakeHiggsfield()
    queue, _ = make_queue(fake, account(tmp_path, balance=1.0))
    with pytest.raises(BudgetExceededError, match="remaining account balance"):
        await provider(tmp_path, queue).reference_to_video(job(), tmp_path)
    assert fake.requests == []


async def test_a_new_process_resumes_the_persisted_request(tmp_path: Path) -> None:
    acct = account(tmp_path)
    fake = FakeHiggsfield(polls_in_progress=10_000)
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
    assert acct.summary().reserved_usd == pytest.approx(1.80)

    fake.polls_in_progress = 0
    second, _ = make_queue(fake, acct)  # nothing in memory
    out = await provider(tmp_path, second).reference_to_video(
        job(remote=RemoteControl(resume=saved[0])), tmp_path
    )
    assert second.submits == 0 and second.resumed == 1 and len(fake.submitted) == 1
    assert out.meta["request_id"] == saved[0].request_id
    assert acct.summary().settled_usd == pytest.approx(1.80) and acct.summary().reserved_usd == 0


async def test_a_resumed_request_that_is_gone_is_resubmitted_once(tmp_path: Path) -> None:
    acct = account(tmp_path)
    stale = acct.reserve(endpoint=EP, est_usd=1.80, ref="dead process")
    gone = RemoteRequest(
        request_id="old",
        status_url=f"{BASE}/requests/old/status",
        response_url=f"{BASE}/requests/old/cancel",
        spend_id=stale,
    )
    fake = FakeHiggsfield(polls_in_progress=0)
    fake.faults["status"] = [httpx.Response(404, json={"detail": "Request not found"})]
    queue, _ = make_queue(fake, acct)
    saved: list[RemoteRequest] = []
    out = await provider(tmp_path, queue).reference_to_video(
        job(remote=RemoteControl(resume=gone, on_submitted=saved.append)), tmp_path
    )
    assert queue.resumed == 1 and queue.submits == 1 and saved[0].request_id == "req1"
    assert out.meta["request_id"] == "req1"
    assert acct.summary().reserved_usd == 0 and acct.summary().settled_usd == pytest.approx(1.80)


async def test_a_resumed_request_that_failed_is_not_resubmitted(tmp_path: Path) -> None:
    acct = account(tmp_path)
    stale = acct.reserve(endpoint=EP, est_usd=1.80, ref="dead process")
    old = RemoteRequest(
        request_id="old",
        status_url=f"{BASE}/requests/old/status",
        response_url=f"{BASE}/requests/old/cancel",
        spend_id=stale,
    )
    fake = FakeHiggsfield(polls_in_progress=0, final="failed")
    queue, _ = make_queue(fake, acct)
    with pytest.raises(PermanentProviderError, match="failed"):
        await provider(tmp_path, queue).reference_to_video(
            job(remote=RemoteControl(resume=old)), tmp_path
        )
    assert queue.submits == 0 and acct.summary().reserved_usd == 0


# ----------------------------------------------------------------------------- runtime wiring


def _hf_runtime(tmp_path: Path, **over: Any) -> AgentRuntime:
    s = settings(tmp_path, ad_provider="higgsfield", hf_key=SECRET, hf_balance_usd=5.0, **over)
    return AgentRuntime.from_settings(
        s, llm_provider=ScriptedLLMProvider.from_scenario("perfume_en")
    )


def test_higgsfield_requires_a_key_and_a_ceiling(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="HF_KEY"):
        settings(tmp_path, ad_provider="higgsfield", hf_balance_usd=5.0)
    with pytest.raises(ValueError, match="HF_BALANCE_USD"):
        settings(tmp_path, ad_provider="higgsfield", hf_key=SECRET)


def test_runtime_builds_the_provider_and_caps_media_by_the_hf_wallet(tmp_path: Path) -> None:
    rt = _hf_runtime(tmp_path, budget_usd=8.0)
    p = rt.higgsfield_video()
    assert p.model == EP and rt.higgsfield_video("seedance_25_ref").endpoint.max_duration_s == 30
    run = Orchestrator(rt).create_run("perfume ad")
    assert run.config.ad_provider == "higgsfield"
    assert run.budget.media_limit_usd == pytest.approx(5.0)  # min(budget 8, wallet 5)
    sid = rt.hf_account().reserve(endpoint=EP, est_usd=3.0, ref="earlier")
    rt.hf_account().settle(sid, 3.0)
    assert Orchestrator(rt).create_run("perfume ad").budget.media_limit_usd == pytest.approx(2.0)
    # the fal rows are a separate wallet
    assert rt.hf_account().summary().settled_usd == pytest.approx(3.0)
    fal_rows = SpendAccount(tmp_path / "acct.db", balance_usd=10.0).summary()
    assert fal_rows.settled_usd == 0


def test_a_real_higgsfield_ad_gets_the_vision_critic(tmp_path: Path) -> None:
    assert settings(tmp_path).resolved_qa_mode() == "mock"
    s = settings(tmp_path, ad_provider="higgsfield", hf_key=SECRET, hf_balance_usd=5.0)
    assert s.media_provider == "mock" and s.resolved_qa_mode() == "llm"
