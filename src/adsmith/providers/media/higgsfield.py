"""Higgsfield media provider over its REST API (ADR-0022): one whole ad from reference images.

Why not the ``higgsfield-client`` SDK: it wraps every request, including the paid submit, in its
own retry loop (3 tries on 408/429/5xx), sends no ``Idempotency-Key`` and has no public switch to
turn that off. That breaks rule 6 (the recovery layer owns every retry) and can start a second
paid generation. This module makes exactly one HTTP request per step, like ``providers/media/fal``:

    upload    POST /files/generate-upload-url → PUT bytes to the presigned url → public_url
    estimate  POST /estimate/{endpoint}       → {"usd": "x.xx"} (same body as the submit)
    submit    POST /{endpoint}                → request_id, status_url, cancel_url
    poll      GET  status_url                 → queued | in_progress | completed | failed | nsfw
                                                | canceled; video.url in the completed body
    download  GET  video.url                  → local file (outputs are deleted after 7 days)

**Estimate first.** A job carries the estimate the human approved; a job without one is never
submitted, and exactly that amount is reserved in the account ledger before the submit.

**No double spend.** The submit sends ``Idempotency-Key`` = sha256(endpoint, body, variant). A
transient failure after the submit keeps the request in memory, so the recovery layer's retry
polls it instead of submitting again; a persisted request (``RemoteControl.resume``) is polled by
a fresh process. An ambiguous submit timeout keeps the reservation open, and a retry of it sends
the same key, so Higgsfield returns the existing request instead of starting a second one.

**Not charged:** ``failed`` and ``nsfw`` (docs, billing-and-retention), so their reservation is
released. **The key** goes only into the ``Authorization`` header of calls to the API host: never
to the presigned upload url, never into a message, a handle or an event.
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

from adsmith.agent.recovery.classifier import classify_higgsfield_http
from adsmith.agent.recovery.errors import (
    AccountLockedError,
    AgentError,
    BudgetExceededError,
    ContentPolicyError,
    PermanentProviderError,
    TransientProviderError,
)
from adsmith.config import HiggsfieldVideoEndpoint
from adsmith.domain.generation import RemoteRequest
from adsmith.providers.media.base import MediaOutput, ReferenceVideoJob, RemoteControl
from adsmith.storage.account import AccountLimitError, SpendAccount

Sleep = Callable[[float], Awaitable[None]]
Probe = Callable[[Path], Awaitable[tuple[int, int, float]]]

_UPLOAD_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"}
UPLOAD_CACHE_S = 50 * 60  # the upload url is valid for 1 h: re-upload well before that
_NOT_FOUND = 404


class RequestGoneError(PermanentProviderError):
    """The status url answered 404: the request no longer exists at Higgsfield."""


@dataclass(frozen=True, slots=True)
class HfHandle:
    request_id: str
    status_url: str
    cancel_url: str
    spend_id: str | None
    key: str = ""  # idempotency key of the body
    resumed: bool = False  # recovered from a persisted RemoteRequest after a crash


def idempotency_key(endpoint: str, body: dict[str, Any], variant: int = 0) -> str:
    blob = json.dumps({"endpoint": endpoint, "body": body, "variant": variant}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


class HiggsfieldQueue:
    """Thin REST client: one request per call, errors classified, never retried here."""

    def __init__(
        self,
        key: str,
        *,
        base_url: str,
        client: httpx.AsyncClient | None = None,
        account: SpendAccount | None = None,
        poll_interval_s: float = 2.0,
        max_poll_interval_s: float = 10.0,
        timeout_s: float = 1800.0,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))
        self._auth = {"Authorization": f"Key {key}"}
        self._base = base_url.rstrip("/")
        self.account = account
        self._poll, self._max_poll, self._timeout = poll_interval_s, max_poll_interval_s, timeout_s
        self._sleep = sleep
        self._clock = clock or (lambda: asyncio.get_running_loop().time())
        self._rng = rng or random.Random(0)
        self._inflight: dict[str, HfHandle] = {}
        self._uploads: dict[str, tuple[str, float]] = {}  # sha256 → (public_url, uploaded at)
        # Reservations kept open by an ambiguous submit, by idempotency key: the retry sends the
        # same key (so it is the same job at Higgsfield) and reuses the same reservation.
        self._ambiguous: dict[str, str | None] = {}
        # Circuit breaker: after an account-level refusal no further paid call is attempted.
        self.paused: AccountLockedError | None = None
        self.submits = 0  # paid submissions made (tests assert no double submit)
        self.resumed = 0  # requests recovered from a persisted RemoteRequest
        self.estimates = 0
        self.uploads = 0

    def __repr__(self) -> str:  # never show the key
        return f"HiggsfieldQueue(base_url={self._base!r}, submits={self.submits})"

    def _api(self, url: str) -> bool:
        return url.startswith(self._base + "/")

    # ------------------------------------------------------------------------ one generation
    async def run(
        self,
        endpoint: str,
        body: dict[str, Any],
        *,
        est_usd: float,
        ref: str,
        remote: RemoteControl | None = None,
        variant: int = 0,
    ) -> tuple[dict[str, Any], HfHandle]:
        """Submit (or resume) → wait → the completed status body. The caller settles the
        reservation once the output is safely stored (``settle``). Resumption order: an in-memory
        handle (a retry in this process); else a persisted ``remote.resume`` (a dead process
        submitted it: poll it); else a new submit, reported through ``remote.on_submitted``
        BEFORE polling. A resumed request that Higgsfield reports gone (404) is resubmitted once
        under the same idempotency key; a failed one is not (it is a result, and it was free)."""
        key = idempotency_key(endpoint, body, variant)
        handle = self._inflight.get(key)
        if handle is None and remote is not None and remote.resume is not None:
            r = remote.resume
            handle = HfHandle(r.request_id, r.status_url, r.response_url, r.spend_id, key, True)
            self._inflight[key] = handle
            self.resumed += 1
        if handle is None:
            handle = await self._submit(endpoint, body, est_usd=est_usd, ref=ref, key=key)
            self._inflight[key] = handle
            if remote is not None and remote.on_submitted is not None:
                remote.on_submitted(
                    RemoteRequest(
                        request_id=handle.request_id,
                        status_url=handle.status_url,
                        response_url=handle.cancel_url,
                        spend_id=handle.spend_id,
                    )
                )
        try:
            result = await self._wait(endpoint, handle)
        except TransientProviderError:
            raise  # keep the handle: a retry resumes this request, it does not resubmit
        except RequestGoneError:
            self.release(handle)
            if handle.resumed and remote is not None:
                fresh = RemoteControl(resume=None, on_submitted=remote.on_submitted)
                return await self.run(
                    endpoint, body, est_usd=est_usd, ref=ref, remote=fresh, variant=variant
                )
            raise
        except AgentError:
            self.release(handle)
            raise
        return result, handle

    def settle(self, handle: HfHandle, actual_usd: float) -> None:
        self._inflight.pop(handle.key, None)
        if self.account is not None and handle.spend_id is not None:
            self.account.settle(handle.spend_id, actual_usd, remote_id=handle.request_id)

    def release(self, handle: HfHandle) -> None:
        self._inflight.pop(handle.key, None)
        if self.account is not None and handle.spend_id is not None:
            self.account.release(handle.spend_id, remote_id=handle.request_id)

    # ------------------------------------------------------------------------ free calls
    async def estimate(self, endpoint: str, body: dict[str, Any]) -> float:
        """The account's own price for exactly this body (no reservation, no generation)."""
        data = await self._post_json(f"{self._base}/estimate/{endpoint}", body, step="estimate")
        self.estimates += 1
        try:
            usd = float(str(data["usd"]))
        except (KeyError, ValueError) as exc:
            raise PermanentProviderError(
                f"higgsfield estimate {endpoint}: no usd in {data}"
            ) from exc
        if usd < 0:
            raise PermanentProviderError(f"higgsfield estimate {endpoint}: negative usd {usd}")
        return usd

    async def upload(self, path: Path) -> str:
        """A public url for a local image (presigned PUT, temporary retention). Cached by content
        hash for 50 minutes, so a retry or a second job within that time does not re-upload."""
        data_bytes = await asyncio.to_thread(path.read_bytes)
        sha = hashlib.sha256(data_bytes).hexdigest()
        cached = self._uploads.get(sha)
        now = self._clock()
        if cached is not None and now - cached[1] < UPLOAD_CACHE_S:
            return cached[0]
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type not in _UPLOAD_TYPES:
            raise PermanentProviderError(
                f"higgsfield upload: {path.name} is {content_type}; allowed: "
                f"{', '.join(sorted(_UPLOAD_TYPES))}"
            )
        slot = await self._post_json(
            f"{self._base}/files/generate-upload-url",
            {"content_type": content_type},
            step="upload",
        )
        try:
            public_url, upload_url = str(slot["public_url"]), str(slot["upload_url"])
        except KeyError as exc:
            raise PermanentProviderError(f"higgsfield upload: unexpected body {slot}") from exc
        headers = {str(k): str(v) for k, v in (slot.get("upload_headers") or {}).items()}
        headers.setdefault("Content-Type", content_type)
        try:  # the presigned url gets the upload headers only, never the API key
            resp = await self._client.put(upload_url, content=data_bytes, headers=headers)
        except httpx.HTTPError as exc:
            raise TransientProviderError(f"higgsfield upload PUT: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise classify_higgsfield_http(
                resp.status_code, resp.text, resp.headers, endpoint="upload PUT"
            )
        self.uploads += 1
        self._uploads[sha] = (public_url, now)
        return public_url

    async def download(self, url: str, path: Path) -> None:
        headers = self._auth if self._api(url) else {}
        try:
            async with self._client.stream("GET", url, headers=headers) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="replace")
                    raise classify_higgsfield_http(
                        resp.status_code, body, resp.headers, endpoint="download"
                    )
                with path.open("wb") as fh:
                    async for chunk in resp.aiter_bytes():
                        fh.write(chunk)
        except httpx.HTTPError as exc:
            raise TransientProviderError(f"higgsfield download: {type(exc).__name__}") from exc

    # ------------------------------------------------------------------------ steps
    async def _submit(
        self, endpoint: str, body: dict[str, Any], *, est_usd: float, ref: str, key: str
    ) -> HfHandle:
        if self.paused is not None:
            raise AccountLockedError(
                f"paid work paused after an account-level refusal: {self.paused}"
            )
        spend_id: str | None = None
        if key in self._ambiguous:
            spend_id = self._ambiguous.pop(key)
        elif self.account is not None:
            try:
                spend_id = self.account.reserve(endpoint=endpoint, est_usd=est_usd, ref=ref)
            except AccountLimitError as exc:
                raise BudgetExceededError(str(exc)) from exc
        headers = {**self._auth, "Idempotency-Key": key}
        try:
            resp = await self._client.post(f"{self._base}/{endpoint}", json=body, headers=headers)
        except httpx.TimeoutException as exc:
            # Ambiguous: the job may be queued. Keep the reservation (fail-safe); a retry sends the
            # same Idempotency-Key, so Higgsfield answers with the existing request, and reuses
            # this reservation instead of holding the money twice.
            self._ambiguous[key] = spend_id
            raise TransientProviderError(
                f"higgsfield submit {endpoint}: timeout (reservation kept: job may be queued)"
            ) from exc
        except httpx.HTTPError as exc:
            self._release_id(spend_id)
            raise TransientProviderError(
                f"higgsfield submit {endpoint}: {type(exc).__name__}"
            ) from exc
        if resp.status_code >= 400:
            self._release_id(spend_id)
            err = classify_higgsfield_http(
                resp.status_code, resp.text, resp.headers, endpoint=endpoint
            )
            if isinstance(err, AccountLockedError):
                self.paused = err
            raise err
        self.submits += 1
        data = resp.json()
        try:
            return HfHandle(
                request_id=str(data["request_id"]),
                status_url=str(data["status_url"]),
                cancel_url=str(data.get("cancel_url") or data["status_url"]),
                spend_id=spend_id,
                key=key,
            )
        except KeyError as exc:
            raise PermanentProviderError(
                f"higgsfield submit {endpoint}: unexpected body {data}"
            ) from exc

    def _release_id(self, spend_id: str | None) -> None:
        if self.account is not None and spend_id is not None:
            self.account.release(spend_id)

    async def _wait(self, endpoint: str, handle: HfHandle) -> dict[str, Any]:
        """Poll with the documented backoff: start at ``poll_interval_s``, grow ×1.5 up to
        ``max_poll_interval_s``, plus up to 25 % jitter (injected rng and sleep)."""
        deadline = self._clock() + self._timeout
        interval = self._poll
        while True:
            status = await self._get_status(endpoint, handle.status_url)
            state = status.get("status")
            if state == "completed":
                return status
            if state == "nsfw":
                raise ContentPolicyError(
                    f"higgsfield {endpoint}: request {handle.request_id} ended as nsfw (not "
                    "charged)",
                    category="higgsfield_nsfw",
                )
            if state in {"failed", "canceled"}:
                why = status.get("error") or "no reason given"
                raise PermanentProviderError(
                    f"higgsfield {endpoint}: request {handle.request_id} {state} (not charged): "
                    f"{str(why)[:300]}"
                )
            if state not in {"queued", "in_progress"}:
                raise PermanentProviderError(f"higgsfield {endpoint}: unexpected status {status}")
            if self._clock() > deadline:
                raise TransientProviderError(
                    f"higgsfield {endpoint}: still {state} after {self._timeout:.0f}s "
                    f"(request {handle.request_id} kept; a retry resumes it)"
                )
            await self._sleep(interval + self._rng.uniform(0.0, interval * 0.25))
            interval = min(self._max_poll, interval * 1.5)

    async def _get_status(self, endpoint: str, url: str) -> dict[str, Any]:
        headers = self._auth if self._api(url) else {}
        try:
            resp = await self._client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise TransientProviderError(
                f"higgsfield {endpoint} status: {type(exc).__name__}"
            ) from exc
        if resp.status_code == _NOT_FOUND:
            raise RequestGoneError(f"higgsfield {endpoint}: request not found at {url}")
        if resp.status_code >= 400:
            raise classify_higgsfield_http(
                resp.status_code, resp.text, resp.headers, endpoint=endpoint
            )
        data: dict[str, Any] = resp.json()
        return data

    async def _post_json(self, url: str, body: dict[str, Any], *, step: str) -> dict[str, Any]:
        try:
            resp = await self._client.post(url, json=body, headers=self._auth)
        except httpx.HTTPError as exc:
            raise TransientProviderError(f"higgsfield {step}: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            err = classify_higgsfield_http(resp.status_code, resp.text, resp.headers, endpoint=step)
            if isinstance(err, AccountLockedError):
                self.paused = err
            raise err
        data: dict[str, Any] = resp.json()
        return data


# ------------------------------------------------------------------------------ provider
class HiggsfieldVideoProvider:
    """Seedance reference-to-video: reference images + one numbered-shot prompt → one ad."""

    name = "higgsfield"

    def __init__(self, queue: HiggsfieldQueue, endpoint: HiggsfieldVideoEndpoint, probe: Probe):
        self.queue, self.endpoint, self.model = queue, endpoint, endpoint.endpoint
        self._probe = probe

    def body(self, job: ReferenceVideoJob) -> dict[str, Any]:
        """The request body, validated against the verified schema BEFORE any HTTP call."""
        ep = self.endpoint
        problems: list[str] = []
        if not ep.min_duration_s <= job.duration_s <= ep.max_duration_s:
            problems.append(
                f"duration {job.duration_s}s outside {ep.min_duration_s}-{ep.max_duration_s}s"
            )
        if job.resolution not in ep.resolutions:
            problems.append(f"resolution {job.resolution!r} not in {ep.resolutions}")
        if job.aspect_ratio not in ep.aspect_ratios:
            problems.append(f"aspect_ratio {job.aspect_ratio!r} not in {ep.aspect_ratios}")
        if not 1 <= len(job.image_urls) <= ep.max_images:
            problems.append(f"{len(job.image_urls)} reference images (allowed 1-{ep.max_images})")
        if any(not u.startswith("https://") for u in job.image_urls):
            problems.append("reference images must be public https urls")
        if not job.prompt.strip():
            problems.append("empty prompt")
        if problems:
            raise PermanentProviderError(f"higgsfield {self.model}: " + "; ".join(problems))
        return {
            **ep.params,
            "prompt": job.prompt,
            "image_urls": list(job.image_urls),
            "duration": int(job.duration_s),
            "resolution": job.resolution,
            "aspect_ratio": job.aspect_ratio,
            "generate_audio": job.generate_audio,
        }

    def check_estimate(self, usd: float, duration_s: int) -> None:
        """Sanity cap: an estimate above ``max_usd_per_second`` × duration is refused."""
        cap = self.endpoint.max_usd_per_second * duration_s
        if usd > cap + 1e-9:
            raise BudgetExceededError(
                f"higgsfield estimate ${usd:.2f} for {duration_s}s exceeds the sanity cap "
                f"${cap:.2f} (max_usd_per_second {self.endpoint.max_usd_per_second:g})"
            )

    async def estimate_usd(self, job: ReferenceVideoJob) -> float:
        usd = await self.queue.estimate(self.model, self.body(job))
        self.check_estimate(usd, job.duration_s)
        return usd

    async def reference_to_video(self, job: ReferenceVideoJob, out_dir: Path) -> MediaOutput:
        if job.est_usd is None:
            raise PermanentProviderError(
                f"higgsfield {self.model}: refusing to submit a job without an approved estimate"
            )
        body = self.body(job)
        self.check_estimate(job.est_usd, job.duration_s)
        lbl = job.label
        result, handle = await self.queue.run(
            self.model,
            body,
            est_usd=job.est_usd,
            ref=f"ad v{lbl.scene_version} a{lbl.attempt_no} c{job.variant}",
            remote=job.remote,
            variant=job.variant,
        )
        video = result.get("video") or {}
        url = video.get("url") if isinstance(video, dict) else None
        if not url:
            self.queue.release(handle)
            raise PermanentProviderError(f"higgsfield {self.model}: no video in {result}")
        path = out_dir / f"ad_v{lbl.scene_version}_a{lbl.attempt_no}_c{job.variant}.mp4"
        await self.queue.download(str(url), path)
        self.queue.settle(handle, job.est_usd)
        w, h, dur = await self._probe(path)
        return MediaOutput(
            path=path,
            kind="video",
            cost_usd=job.est_usd,
            width=w,
            height=h,
            duration_s=dur,
            remote_url=str(url),
            meta={"request_id": handle.request_id, "expires_at": result.get("expires_at")},
        )
