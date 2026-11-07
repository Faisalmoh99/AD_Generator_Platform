"""Kie.ai media provider over its REST API (ADR-0027): one whole ad from reference images.

Same shape as ``providers/media/higgsfield``: exactly one HTTP request per step, every error
classified (``classify_kie``), every retry owned by ``agent/recovery``:

    upload    POST {upload}/api/file-stream-upload   → data.downloadUrl (public, deleted after 24 h)
    submit    POST {api}/api/v1/jobs/createTask      → data.taskId
    poll      GET  {api}/api/v1/jobs/recordInfo      → waiting | queuing | generating | success |
                                                       fail; resultJson is a JSON *string*
    download  GET  resultUrls[0]                     → local file (kept 14 days at Kie)
    balance   GET  {api}/api/v1/chat/credit          → remaining credits (free)

**HTTP 200 is not success.** Kie answers errors with HTTP 200 and a body ``code`` (401 key, 402
credits, 422 validation, 429 rate limit, 455 maintenance, 500, 501 generation failed): every
response's ``code`` is checked.

**Price.** Kie publishes a per-second credit rate; the quote is computed locally from the catalog
(``credit_quote``) and the wallet is settled at the task's own ``creditsConsumed`` (the credits
Kie actually deducted), so the ledger matches the console without a manual ``settle-actual``.

**No idempotency key.** Kie's createTask has none, so a submit whose answer is lost may have
started a paid task. That case is never retried: the reservation stays in the wallet, the queue
pauses and ``AmbiguousSubmitError`` (an account-level refusal) pauses the run; on resume the
generation runner refuses to submit that version again (the user's rule, 2026-11-07). A submit
that never left the machine (connect error) is released and retried as transient. A task id is
reported through ``RemoteControl.on_submitted`` before polling, so a crash resumes by polling.

**The key** goes only into the ``Authorization`` header of calls to the two Kie hosts: never into
a message, a handle, an event or a download from another host.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from adsmith.agent.recovery.classifier import classify_kie
from adsmith.agent.recovery.errors import (
    AccountLockedError,
    AgentError,
    BudgetExceededError,
    ContentPolicyError,
    PermanentProviderError,
    RateLimitedError,
    TransientProviderError,
)
from adsmith.config import AdVideoEndpoint
from adsmith.domain.generation import RemoteRequest
from adsmith.providers.media.base import (
    AMBIGUOUS_SUBMIT,
    MediaOutput,
    PriceQuote,
    ReferenceVideoJob,
    RemoteControl,
    credit_quote,
)
from adsmith.storage.account import AccountLimitError, SpendAccount

Sleep = Callable[[float], Awaitable[None]]
Probe = Callable[[Path], Awaitable[tuple[int, int, float]]]

UPLOAD_CACHE_S = 20 * 3600  # uploaded files are deleted after 24 h: re-upload well before
UPLOAD_PATH = "adsmith"
_UPLOAD_TYPES = {"image/jpeg", "image/png", "image/webp", "image/bmp", "image/tiff", "image/gif"}
_OK = 200
_NOT_FOUND = 422  # recordInfo answers 422 "recordInfo is null" for an unknown task
_RUNNING = {"waiting", "queuing", "generating"}
_POLICY = ("nsfw", "sensitive", "policy", "moderation", "safety", "flagged", "violat")


class AmbiguousSubmitError(AccountLockedError):
    """The createTask answer was lost after the request was sent: a paid task may exist."""


class TaskGoneError(PermanentProviderError):
    """recordInfo does not know the task id (422 "recordInfo is null")."""


@dataclass(frozen=True, slots=True)
class KieHandle:
    task_id: str
    status_url: str
    spend_id: str | None
    key: str = ""  # hash of (model, input): the in-process resume key
    resumed: bool = False


def task_key(model: str, input_: dict[str, Any]) -> str:
    blob = json.dumps({"model": model, "input": input_}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


class KieQueue:
    """Thin REST client: one request per call, errors classified, never retried here."""

    def __init__(
        self,
        key: str,
        *,
        base_url: str,
        upload_base_url: str,
        client: httpx.AsyncClient | None = None,
        account: SpendAccount | None = None,
        poll_interval_s: float = 3.0,
        max_poll_interval_s: float = 15.0,
        timeout_s: float = 1800.0,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))
        self._auth = {"Authorization": f"Bearer {key}"}
        self._base = base_url.rstrip("/")
        self._upload_base = upload_base_url.rstrip("/")
        self.account = account
        self._poll, self._max_poll, self._timeout = poll_interval_s, max_poll_interval_s, timeout_s
        self._sleep = sleep
        self._clock = clock or (lambda: asyncio.get_running_loop().time())
        self._rng = rng or random.Random(0)
        self._inflight: dict[str, KieHandle] = {}
        self._uploads: dict[str, tuple[str, float]] = {}  # sha256 → (downloadUrl, uploaded at)
        # Circuit breaker: after an account-level refusal (or an ambiguous submit) no further
        # paid call is attempted by this process.
        self.paused: AccountLockedError | None = None
        self.submits = 0
        self.resumed = 0
        self.uploads = 0

    def __repr__(self) -> str:  # never show the key
        return f"KieQueue(base_url={self._base!r}, submits={self.submits})"

    def _ours(self, url: str) -> bool:
        return url.startswith((self._base + "/", self._upload_base + "/"))

    # ------------------------------------------------------------------------ one generation
    async def run(
        self,
        model: str,
        input_: dict[str, Any],
        *,
        est_usd: float,
        ref: str,
        remote: RemoteControl | None = None,
    ) -> tuple[dict[str, Any], KieHandle]:
        """Submit (or resume) → wait → the success record. The caller settles the reservation
        once the output is stored. Resumption: an in-memory handle (a retry in this process),
        else a persisted ``remote.resume`` (poll it), else a new submit reported through
        ``remote.on_submitted`` BEFORE polling. Nothing is ever resubmitted automatically."""
        key = task_key(model, input_)
        handle = self._inflight.get(key)
        if handle is None and remote is not None and remote.resume is not None:
            r = remote.resume
            handle = KieHandle(r.request_id, r.status_url, r.spend_id, key, resumed=True)
            self._inflight[key] = handle
            self.resumed += 1
        if handle is None:
            handle = await self._submit(model, input_, est_usd=est_usd, ref=ref, key=key)
            self._inflight[key] = handle
            if remote is not None and remote.on_submitted is not None:
                remote.on_submitted(
                    RemoteRequest(
                        request_id=handle.task_id,
                        status_url=handle.status_url,
                        response_url=handle.status_url,
                        spend_id=handle.spend_id,
                    )
                )
        try:
            record = await self._wait(model, handle)
        except (TransientProviderError, RateLimitedError):
            raise  # keep the handle: a retry polls the same task, it never resubmits
        except AgentError:
            self.release(handle)
            raise
        return record, handle

    def settle(self, handle: KieHandle, actual_usd: float) -> None:
        self._inflight.pop(handle.key, None)
        if self.account is not None and handle.spend_id is not None:
            self.account.settle(handle.spend_id, actual_usd, remote_id=handle.task_id)

    def release(self, handle: KieHandle) -> None:
        self._inflight.pop(handle.key, None)
        if self.account is not None and handle.spend_id is not None:
            self.account.release(handle.spend_id, remote_id=handle.task_id)

    # ------------------------------------------------------------------------ free calls
    async def credits(self) -> float:
        """Remaining credits of the account (free)."""
        data = await self._call("GET", f"{self._base}/api/v1/chat/credit", step="credits")
        try:
            return float(data)
        except (TypeError, ValueError) as exc:
            raise PermanentProviderError(f"kie credits: unexpected data {data!r}") from exc

    async def upload(self, path: Path) -> str:
        """A public url for a local image (stream upload, free, deleted by Kie after 24 h).
        Cached by content hash for 20 hours."""
        data_bytes = await asyncio.to_thread(path.read_bytes)
        sha = hashlib.sha256(data_bytes).hexdigest()
        cached = self._uploads.get(sha)
        now = self._clock()
        if cached is not None and now - cached[1] < UPLOAD_CACHE_S:
            return cached[0]
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type not in _UPLOAD_TYPES:
            raise PermanentProviderError(
                f"kie upload: {path.name} is {content_type}; allowed: "
                f"{', '.join(sorted(_UPLOAD_TYPES))}"
            )
        name = f"{sha[:24]}{path.suffix.lower()}"
        data = await self._call(
            "POST",
            f"{self._upload_base}/api/file-stream-upload",
            files={"file": (name, data_bytes, content_type)},
            form={"uploadPath": UPLOAD_PATH, "fileName": name},
            step="upload",
        )
        url = data.get("downloadUrl") if isinstance(data, dict) else None
        if not isinstance(url, str) or not url.startswith("https://"):
            raise PermanentProviderError(f"kie upload: no https downloadUrl in {data!r}")
        self.uploads += 1
        self._uploads[sha] = (url, now)
        return url

    async def download(self, url: str, path: Path) -> None:
        headers = self._auth if self._ours(url) else {}
        try:
            async with self._client.stream("GET", url, headers=headers) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="replace")
                    raise classify_kie(resp.status_code, body, resp.headers, step="download")
                with path.open("wb") as fh:
                    async for chunk in resp.aiter_bytes():
                        fh.write(chunk)
        except httpx.HTTPError as exc:
            raise TransientProviderError(f"kie download: {type(exc).__name__}") from exc

    # ------------------------------------------------------------------------ steps
    async def _submit(
        self, model: str, input_: dict[str, Any], *, est_usd: float, ref: str, key: str
    ) -> KieHandle:
        if self.paused is not None:
            raise AccountLockedError(
                f"paid work paused after an account-level refusal: {self.paused.message}"
            )
        spend_id: str | None = None
        if self.account is not None:
            try:
                spend_id = self.account.reserve(endpoint=model, est_usd=est_usd, ref=ref)
            except AccountLimitError as exc:
                raise BudgetExceededError(str(exc)) from exc
        body = {"model": model, "input": input_}
        try:
            resp = await self._client.post(
                f"{self._base}/api/v1/jobs/createTask", json=body, headers=self._auth
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            self._release_id(spend_id)  # never reached Kie: nothing can have started
            raise TransientProviderError(f"kie submit: {type(exc).__name__}") from exc
        except httpx.HTTPError as exc:
            # Sent, answer lost: a paid task may exist and Kie has no idempotency key. Keep the
            # reservation (fail-safe), pause this process and never resubmit automatically.
            err = AmbiguousSubmitError(
                f"{AMBIGUOUS_SUBMIT} on {model} ({type(exc).__name__}): a paid task may have "
                "started. The reservation is kept and nothing is resubmitted; compare "
                "`adsmith kie-balance` with the console, then `adsmith regenerate-ad` if no "
                "task ran"
            )
            self.paused = err
            raise err from exc
        try:
            data = self._checked(resp, step="submit")
        except AgentError as err:
            self._release_id(spend_id)
            if isinstance(err, AccountLockedError):
                self.paused = err
            raise
        task_id = data.get("taskId") if isinstance(data, dict) else None
        if not isinstance(task_id, str) or not task_id:
            # Kie said 200 but gave no id: treat as ambiguous (it may have started).
            missing = AmbiguousSubmitError(
                f"{AMBIGUOUS_SUBMIT} on {model}: code 200 without a taskId ({data!r})"
            )
            self.paused = missing
            raise missing
        self.submits += 1
        status_url = f"{self._base}/api/v1/jobs/recordInfo?taskId={task_id}"
        return KieHandle(task_id=task_id, status_url=status_url, spend_id=spend_id, key=key)

    def _release_id(self, spend_id: str | None) -> None:
        if self.account is not None and spend_id is not None:
            self.account.release(spend_id)

    async def _wait(self, model: str, handle: KieHandle) -> dict[str, Any]:
        """Poll from ``poll_interval_s``, ×1.5 up to ``max_poll_interval_s``, + ≤ 25 % jitter."""
        deadline = self._clock() + self._timeout
        interval = self._poll
        while True:
            try:
                rec = await self._call("GET", handle.status_url, step="status")
            except PermanentProviderError as exc:
                if "recordInfo is null" in exc.message:
                    raise TaskGoneError(
                        f"kie {model}: task {handle.task_id} is unknown to Kie"
                    ) from exc
                raise
            if not isinstance(rec, dict):
                raise PermanentProviderError(f"kie {model}: unexpected record {rec!r}")
            state = rec.get("state")
            if state == "success":
                return rec
            if state == "fail":
                why = f"{rec.get('failCode') or ''} {rec.get('failMsg') or 'no reason given'}"
                msg = f"kie {model}: task {handle.task_id} failed (not charged): {why[:300]}"
                if any(m in why.lower() for m in _POLICY):
                    raise ContentPolicyError(msg, category="kie_safety")
                raise PermanentProviderError(msg)
            if state not in _RUNNING:
                raise PermanentProviderError(f"kie {model}: unexpected state {state!r}")
            if self._clock() > deadline:
                raise TransientProviderError(
                    f"kie {model}: still {state} after {self._timeout:.0f}s (task "
                    f"{handle.task_id} kept; a retry resumes it)"
                )
            await self._sleep(interval + self._rng.uniform(0.0, interval * 0.25))
            interval = min(self._max_poll, interval * 1.5)

    async def _call(
        self,
        method: str,
        url: str,
        *,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        form: dict[str, str] | None = None,
        step: str,
    ) -> Any:
        try:
            resp = await self._client.request(
                method, url, headers=self._auth, files=files, data=form
            )
        except httpx.HTTPError as exc:
            raise TransientProviderError(f"kie {step}: {type(exc).__name__}") from exc
        try:
            return self._checked(resp, step=step)
        except AccountLockedError as err:
            self.paused = err
            raise

    @staticmethod
    def _checked(resp: httpx.Response, *, step: str) -> Any:
        if resp.status_code >= 400:
            raise classify_kie(resp.status_code, resp.text, resp.headers, step=step)
        try:
            body = resp.json()
        except ValueError as exc:
            raise TransientProviderError(f"kie {step}: the answer is not JSON") from exc
        code = body.get("code") if isinstance(body, dict) else None
        if not isinstance(code, int):
            raise PermanentProviderError(f"kie {step}: no code in {str(body)[:300]}")
        if code != _OK:
            raise classify_kie(code, str(body.get("msg") or ""), resp.headers, step=step)
        return body.get("data")


def result_url(record: dict[str, Any]) -> str | None:
    """The first result url: ``resultJson`` is a JSON string (``response`` is its parsed form)."""
    parsed: object = record.get("response")
    raw = record.get("resultJson")
    if not isinstance(parsed, dict) and isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
    if isinstance(parsed, dict):
        urls = parsed.get("resultUrls")
        if isinstance(urls, list) and urls and isinstance(urls[0], str):
            return urls[0]
    return None


# ------------------------------------------------------------------------------ provider
class KieVideoProvider:
    """Seedance 2.0 reference-to-video on Kie: reference images + one prompt → one ad."""

    name = "kie"

    def __init__(self, queue: KieQueue, endpoint: AdVideoEndpoint, probe: Probe):
        if endpoint.credit_pricing is None:
            raise ValueError(f"kie endpoint {endpoint.endpoint} has no credit_pricing")
        self.queue, self.endpoint, self.model = queue, endpoint, endpoint.endpoint
        self.pricing = endpoint.credit_pricing
        self.max_prompt_chars = endpoint.max_prompt_chars
        self._probe = probe

    async def reference_url(self, path: Path) -> str:
        return await self.queue.upload(path)

    def body(self, job: ReferenceVideoJob) -> dict[str, Any]:
        """The ``input`` object, validated against the verified schema BEFORE any HTTP call."""
        ep = self.endpoint
        problems: list[str] = []
        if not ep.min_duration_s <= job.duration_s <= ep.max_duration_s:
            problems.append(
                f"duration {job.duration_s}s outside {ep.min_duration_s}-{ep.max_duration_s}s"
            )
        if job.resolution not in ep.resolutions:
            problems.append(f"resolution {job.resolution!r} not priced/offered: {ep.resolutions}")
        if job.aspect_ratio not in ep.aspect_ratios:
            problems.append(f"aspect_ratio {job.aspect_ratio!r} not in {ep.aspect_ratios}")
        if not 1 <= len(job.image_urls) <= ep.max_images:
            problems.append(f"{len(job.image_urls)} reference images (allowed 1-{ep.max_images})")
        if any(not u.startswith("https://") for u in job.image_urls):
            problems.append("reference images must be public https urls")
        if len(job.prompt.strip()) < 3:
            problems.append("prompt shorter than 3 characters")
        if len(job.prompt) > self.max_prompt_chars:
            problems.append(f"prompt {len(job.prompt)} > {self.max_prompt_chars} characters")
        if problems:
            raise PermanentProviderError(f"kie {self.model}: " + "; ".join(problems))
        return {
            **ep.params,
            "prompt": job.prompt,
            "reference_image_urls": list(job.image_urls),
            "generate_audio": job.generate_audio,
            "resolution": job.resolution,
            "aspect_ratio": job.aspect_ratio,
            "duration": int(job.duration_s),
        }

    def check_estimate(self, usd: float, duration_s: int) -> None:
        cap = self.endpoint.max_usd_per_second * duration_s
        if usd > cap + 1e-9:
            raise BudgetExceededError(
                f"kie price ${usd:.2f} for {duration_s}s exceeds the sanity cap ${cap:.2f}"
            )

    async def quote(self, job: ReferenceVideoJob) -> PriceQuote:
        """The published per-second credit rate × the duration (no API call; nothing reserved)."""
        self.body(job)
        q = credit_quote(self.pricing, resolution=job.resolution, duration_s=job.duration_s)
        self.check_estimate(q.reserved_usd, job.duration_s)
        return q

    def price_check(
        self, job: ReferenceVideoJob, consumed: float | None, w: int, h: int, seconds: float
    ) -> dict[str, object]:
        computed_credits = self.pricing.credits(job.resolution, job.duration_s)
        check: dict[str, object] = {
            "computed_usd": job.computed_usd,
            "reserved_usd": job.est_usd,
            "credits_computed": computed_credits,
            "credits_consumed": consumed,
            "actual_size": f"{w}x{h}",
            "actual_duration_s": round(seconds, 3),
        }
        if consumed is not None:
            check["credits_match"] = abs(consumed - computed_credits) < 0.05
        return check

    async def reference_to_video(self, job: ReferenceVideoJob, out_dir: Path) -> MediaOutput:
        if job.est_usd is None:
            raise PermanentProviderError(
                f"kie {self.model}: refusing to submit a job without an approved estimate"
            )
        input_ = self.body(job)
        self.check_estimate(job.est_usd, job.duration_s)
        lbl = job.label
        record, handle = await self.queue.run(
            self.model,
            input_,
            est_usd=job.est_usd,
            ref=f"ad v{lbl.scene_version} a{lbl.attempt_no} c{job.variant}",
            remote=job.remote,
        )
        url = result_url(record)
        if not url:
            self.queue.release(handle)
            raise PermanentProviderError(f"kie {self.model}: no result url in {record}")
        path = out_dir / f"ad_v{lbl.scene_version}_a{lbl.attempt_no}_c{job.variant}.mp4"
        await self.queue.download(url, path)
        raw = record.get("creditsConsumed")
        consumed = float(raw) if isinstance(raw, int | float) and raw > 0 else None
        computed = job.computed_usd if job.computed_usd is not None else job.est_usd
        cost = round(consumed * self.pricing.usd_per_credit, 6) if consumed else computed
        self.queue.settle(handle, cost)
        w, h, dur = await self._probe(path)
        return MediaOutput(
            path=path,
            kind="video",
            cost_usd=cost,
            width=w,
            height=h,
            duration_s=dur,
            remote_url=url,
            meta={
                "request_id": handle.task_id,
                "credits_consumed": consumed,
                "price_check": self.price_check(job, consumed, w, h, dur),
            },
        )
