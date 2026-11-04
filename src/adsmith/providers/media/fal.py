"""fal.ai media providers over fal's queue REST API (schemas verified in M11, see models.toml).

Why not ``fal-client``: it retries internally (hard-coded attempts, including 409/429 on
*submit*). That breaks rule 6 (the recovery layer owns every retry) and, worse, a retried submit
can start a second paid generation. This module makes exactly one HTTP request per step:

    submit   POST {queue}/{endpoint}           → request_id, status_url, response_url
    poll     GET  status_url                   → IN_QUEUE | IN_PROGRESS | COMPLETED
    result   GET  response_url                 → model output (or a typed error)
    download GET  output file URL              → local file

**No double spend.** A paid job is submitted once per distinct request. If anything *after* the
submit fails transiently (a poll, the result fetch, the download), the handle is kept in memory
under the request's idempotency key and the error is raised as transient. When the recovery layer
retries the same job, the provider resumes the *existing* request instead of submitting again.

**Wallet ceiling.** Every submit first reserves its estimate in the account ledger
(``storage/account.py``) and is refused if it would exceed the remaining prepaid balance; it is
settled at the actual cost on success and released on a definitive failure. An ambiguous submit
failure (a timeout where the job may or may not have been queued) keeps the reservation open, so
uncertain money counts as spent.

Inputs are sent as base64 data URIs (accepted for fal file inputs), so no separate upload step
and no public URL to our keyframes.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import mimetypes
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from adsmith.agent.recovery.classifier import classify_fal_http
from adsmith.agent.recovery.errors import (
    AgentError,
    BudgetExceededError,
    ContentPolicyError,
    PermanentProviderError,
    TransientProviderError,
)
from adsmith.config import FalImageEndpoint, FalVideoEndpoint
from adsmith.providers.media.base import ImageJob, MediaOutput, VideoCapabilities, VideoJob
from adsmith.storage.account import AccountLimitError, SpendAccount

QUEUE_BASE = "https://queue.fal.run"
Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class FalHandle:
    request_id: str
    status_url: str
    response_url: str
    spend_id: str | None
    key: str = ""  # idempotency key of the payload


def data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def _idempotency_key(endpoint: str, payload: dict[str, Any]) -> str:
    blob = json.dumps({"endpoint": endpoint, "payload": payload}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


class FalQueue:
    """Thin queue client: one request per call, errors classified, never retried here."""

    def __init__(
        self,
        key: str,
        *,
        client: httpx.AsyncClient | None = None,
        account: SpendAccount | None = None,
        poll_interval_s: float = 2.0,
        timeout_s: float = 900.0,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] | None = None,
        base_url: str = QUEUE_BASE,
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))
        self._headers = {"Authorization": f"Key {key}"}
        self.account = account
        self._poll, self._timeout, self._sleep = poll_interval_s, timeout_s, sleep
        self._clock = clock or (lambda: asyncio.get_running_loop().time())
        self._base = base_url.rstrip("/")
        self._inflight: dict[str, FalHandle] = {}
        self.submits = 0  # paid submissions made (tests assert no double submit)

    # ------------------------------------------------------------------------ one generation
    async def run(
        self, endpoint: str, payload: dict[str, Any], *, est_usd: float, ref: str
    ) -> tuple[dict[str, Any], FalHandle]:
        """Submit (or resume) → wait → result. Returns the output JSON and the handle; the caller
        settles the reservation once the output is safely stored (``settle``)."""
        key = _idempotency_key(endpoint, payload)
        handle = self._inflight.get(key)
        if handle is None:
            handle = await self._submit(endpoint, payload, est_usd=est_usd, ref=ref, key=key)
            self._inflight[key] = handle
        try:
            await self._wait(endpoint, handle)
            result = await self._get_json(endpoint, handle.response_url)
        except TransientProviderError:
            raise  # keep the handle: a retry resumes this request, it does not resubmit
        except AgentError:
            self.release(handle)
            raise
        # Still in flight until the caller has the file: a transient download failure must
        # resume this request on retry, not resubmit it. settle()/release() end it.
        return result, handle

    def settle(self, handle: FalHandle, actual_usd: float) -> None:
        self._inflight.pop(handle.key, None)
        if self.account is not None and handle.spend_id is not None:
            self.account.settle(handle.spend_id, actual_usd, remote_id=handle.request_id)

    def release(self, handle: FalHandle) -> None:
        self._inflight.pop(handle.key, None)
        if self.account is not None and handle.spend_id is not None:
            self.account.release(handle.spend_id, remote_id=handle.request_id)

    async def download(self, url: str, path: Path) -> None:
        try:
            async with self._client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="replace")
                    raise classify_fal_http(resp.status_code, body, resp.headers, endpoint="cdn")
                with path.open("wb") as fh:
                    async for chunk in resp.aiter_bytes():
                        fh.write(chunk)
        except httpx.HTTPError as exc:
            raise TransientProviderError(f"fal download: {type(exc).__name__}: {exc}") from exc

    # ------------------------------------------------------------------------ steps
    async def _submit(
        self, endpoint: str, payload: dict[str, Any], *, est_usd: float, ref: str, key: str
    ) -> FalHandle:
        spend_id = None
        if self.account is not None:
            try:
                spend_id = self.account.reserve(endpoint=endpoint, est_usd=est_usd, ref=ref)
            except AccountLimitError as exc:
                raise BudgetExceededError(str(exc)) from exc
        try:
            resp = await self._client.post(
                f"{self._base}/{endpoint}", json=payload, headers=self._headers
            )
        except httpx.TimeoutException as exc:
            # Ambiguous: the job may be queued and billed. Keep the reservation open (fail-safe).
            raise TransientProviderError(
                f"fal submit {endpoint}: timeout (reservation kept: job may have been queued)"
            ) from exc
        except httpx.HTTPError as exc:
            self._release_id(spend_id)
            raise TransientProviderError(f"fal submit {endpoint}: {exc}") from exc
        if resp.status_code >= 400:
            self._release_id(spend_id)
            raise classify_fal_http(resp.status_code, resp.text, resp.headers, endpoint=endpoint)
        self.submits += 1
        body = resp.json()
        try:
            return FalHandle(
                request_id=str(body["request_id"]),
                status_url=str(body["status_url"]),
                response_url=str(body["response_url"]),
                spend_id=spend_id,
                key=key,
            )
        except KeyError as exc:
            raise PermanentProviderError(f"fal submit {endpoint}: unexpected body {body}") from exc

    def _release_id(self, spend_id: str | None) -> None:
        if self.account is not None and spend_id is not None:
            self.account.release(spend_id)

    async def _wait(self, endpoint: str, handle: FalHandle) -> None:
        deadline = self._clock() + self._timeout
        while True:
            status = await self._get_json(endpoint, handle.status_url)
            state = status.get("status")
            if state == "COMPLETED":
                return
            if state not in {"IN_QUEUE", "IN_PROGRESS"}:
                raise PermanentProviderError(f"fal {endpoint}: unexpected status {status}")
            if self._clock() > deadline:
                raise TransientProviderError(
                    f"fal {endpoint}: still {state} after {self._timeout:.0f}s "
                    f"(request {handle.request_id} kept; a retry resumes it)"
                )
            await self._sleep(self._poll)

    async def _get_json(self, endpoint: str, url: str) -> dict[str, Any]:
        try:
            resp = await self._client.get(url, headers=self._headers)
        except httpx.HTTPError as exc:
            raise TransientProviderError(f"fal {endpoint}: {type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400:
            raise classify_fal_http(resp.status_code, resp.text, resp.headers, endpoint=endpoint)
        data: dict[str, Any] = resp.json()
        return data


# ------------------------------------------------------------------------------ providers
def _ext(url: str, default: str) -> str:
    tail = url.rsplit("?", 1)[0].rsplit(".", 1)
    return f".{tail[1]}" if len(tail) == 2 and len(tail[1]) <= 4 else default


class FalImageProvider:
    name = "fal"

    def __init__(self, queue: FalQueue, endpoint: FalImageEndpoint, *, steps: int = 28) -> None:
        self.queue, self.endpoint, self.model = queue, endpoint, endpoint.endpoint
        self.steps = steps

    def estimate_usd(self, width: int, height: int) -> float:
        return self.endpoint.usd_for(width, height)

    async def generate_image(self, job: ImageJob, out_dir: Path) -> MediaOutput:
        # FLUX dev has no negative_prompt field (schema verified): the positive prompt stands alone.
        payload: dict[str, Any] = {
            "prompt": job.prompt,
            "image_size": {"width": job.width, "height": job.height},
            "num_inference_steps": self.steps,
            "seed": job.seed,
            "num_images": 1,
            "output_format": "png",
            "enable_safety_checker": True,
        }
        lbl = job.label
        est = self.estimate_usd(job.width, job.height)
        result, handle = await self.queue.run(
            self.model,
            payload,
            est_usd=est,
            ref=f"s{lbl.scene_order}v{lbl.scene_version} keyframe a{lbl.attempt_no}",
        )
        nsfw = result.get("has_nsfw_concepts") or []
        images = result.get("images") or []
        if any(nsfw):
            self.queue.settle(handle, est)  # the safety-blanked image is still billed
            raise ContentPolicyError(
                "fal: safety checker flagged the keyframe", category="fal_safety"
            )
        if not images:
            self.queue.release(handle)
            raise PermanentProviderError(f"fal {self.model}: no image in result")
        img = images[0]
        path = out_dir / f"kf_s{lbl.scene_order}_v{lbl.scene_version}_a{lbl.attempt_no}.png"
        await self.queue.download(str(img["url"]), path)
        self.queue.settle(handle, est)
        return MediaOutput(
            path=path,
            kind="image",
            cost_usd=est,
            width=int(img.get("width") or job.width),
            height=int(img.get("height") or job.height),
            remote_url=str(img["url"]),
            meta={"request_id": handle.request_id, "seed": result.get("seed")},
        )


class FalVideoProvider:
    name = "fal"

    def __init__(
        self,
        queue: FalQueue,
        endpoint: FalVideoEndpoint,
        capabilities: VideoCapabilities,
        probe: Callable[[Path], Awaitable[tuple[int, int, float]]],
    ) -> None:
        self.queue, self.endpoint, self.model = queue, endpoint, endpoint.endpoint
        self.usd_per_second = endpoint.usd_per_second
        self.capabilities = capabilities
        self._probe = probe

    async def image_to_video(self, job: VideoJob, out_dir: Path) -> MediaOutput:
        payload: dict[str, Any] = {
            **job.params,
            self.endpoint.image_field: await asyncio.to_thread(data_uri, job.image_path),
            "prompt": job.prompt,
        }
        if job.negative_prompt and self.capabilities.supports_negative:
            payload["negative_prompt"] = job.negative_prompt
        lbl = job.label
        est = round(self.usd_per_second * math.ceil(job.duration_s), 6)
        result, handle = await self.queue.run(
            self.model,
            payload,
            est_usd=est,
            ref=f"s{lbl.scene_order}v{lbl.scene_version} clip a{lbl.attempt_no}",
        )
        video = result.get("video") or {}
        url = video.get("url")
        if not url:
            self.queue.release(handle)
            raise PermanentProviderError(f"fal {self.model}: no video in result: {result}")
        stem = f"clip_s{lbl.scene_order}_v{lbl.scene_version}_a{lbl.attempt_no}"
        path = out_dir / f"{stem}{_ext(str(url), '.mp4')}"
        await self.queue.download(str(url), path)
        self.queue.settle(handle, est)
        w, h, dur = await self._probe(path)
        return MediaOutput(
            path=path,
            kind="video",
            cost_usd=est,
            width=w,
            height=h,
            duration_s=dur,
            remote_url=str(url),
            meta={"request_id": handle.request_id},
        )
