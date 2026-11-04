"""Crash safety with (fake) fal through the real scene pipeline: a process killed after a paid
submit is resumed by a fresh process, which polls the persisted request instead of paying again."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import httpx

from adsmith.agent.context import AgentRuntime, video_capabilities
from adsmith.agent.media import MediaGateway
from adsmith.agent.orchestrator import Orchestrator
from adsmith.domain.enums import ScenePhase, SceneStatus, Stage
from adsmith.domain.run import RunState
from adsmith.providers.adapters.base import ADAPTERS
from adsmith.providers.llm.scripted import ScriptedLLMProvider
from adsmith.providers.media.chaos import ChaosPolicy
from adsmith.providers.media.fal import FalImageProvider, FalQueue, FalVideoProvider
from tests.media_helpers import REQUEST, fast_settings


class FakeFalServer:
    """Per-request state; video jobs stay IN_PROGRESS while ``video_held`` (the crash window)."""

    def __init__(self) -> None:
        self.kinds: dict[str, str] = {}
        self.video_held = True
        self.submits: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST":
            body = json.loads(request.content)
            kind = "image" if "image_size" in body else "video"
            rid = f"req{len(self.submits) + 1}"
            self.submits.append(kind)
            self.kinds[rid] = kind
            base = f"https://queue.fal.run/x/requests/{rid}"
            return httpx.Response(
                200, json={"request_id": rid, "status_url": f"{base}/status", "response_url": base}
            )
        if url.startswith("https://cdn.test/"):
            return httpx.Response(200, content=b"\x89PNG fake media")
        rid = url.split("/requests/")[1].split("/", maxsplit=1)[0]
        if url.endswith("/status"):
            held = self.kinds[rid] == "video" and self.video_held
            return httpx.Response(200, json={"status": "IN_PROGRESS" if held else "COMPLETED"})
        if self.kinds[rid] == "image":
            return httpx.Response(
                200,
                json={
                    "images": [{"url": f"https://cdn.test/{rid}.png"}],
                    "has_nsfw_concepts": [False],
                },
            )
        return httpx.Response(200, json={"video": {"url": f"https://cdn.test/{rid}.mp4"}})


def runtime(tmp_path: Path, server: FakeFalServer) -> tuple[AgentRuntime, FalQueue]:
    settings = fast_settings(tmp_path)
    rt = AgentRuntime.from_settings(
        settings, llm_provider=ScriptedLLMProvider.from_scenario("perfume_en")
    )

    async def yield_once(_: float) -> None:
        await asyncio.sleep(0)

    rt.sleep = yield_once
    queue = FalQueue(
        "test", client=httpx.AsyncClient(transport=httpx.MockTransport(server.handler)),
        poll_interval_s=0.0, timeout_s=1e9, sleep=yield_once,
    )  # fmt: skip
    fal = settings.catalog.fal
    vid = fal.video.endpoints["kling_v3_pro"]

    async def probe(_: Path) -> tuple[int, int, float]:
        return 320, 568, 4.0

    def factory(run: RunState) -> MediaGateway:
        return MediaGateway(
            image=FalImageProvider(queue, fal.image.endpoints["flux_dev"]),
            video=FalVideoProvider(queue, vid, video_capabilities(vid), probe),
            adapter=ADAPTERS["kling"],
            storage=rt.storage,
            chaos=ChaosPolicy(settings.chaos_config, enabled=False),
            long_side=settings.media_long_side,
        )

    rt.media_factory = factory
    return rt, queue


async def test_killed_after_submit_then_resumed_pays_once(tmp_path: Path) -> None:
    server = FakeFalServer()
    rt1, _ = runtime(tmp_path, server)
    orch1 = Orchestrator(rt1)
    run = orch1.create_run(REQUEST, interactive=False)
    crashed = asyncio.ensure_future(orch1.run_until_blocked(run.id, stop_before=Stage.QA))
    while "video" not in server.submits:  # noqa: ASYNC110 - first paid clip submitted
        await asyncio.sleep(0)
    for _ in range(20):
        await asyncio.sleep(0)
    crashed.cancel()  # the process dies while the clips are rendering at fal
    with contextlib.suppress(asyncio.CancelledError):
        await crashed
    killed = rt1.storage.runs.load(run.id)
    assert killed.storyboard is not None
    persisted = [
        a.remote_request for s in killed.storyboard.scenes for a in s.attempts
        if a.phase is ScenePhase.CLIP and a.remote_request is not None
    ]  # fmt: skip
    assert persisted, "the submitted clip's request id must be on disk before polling"
    submits_before = list(server.submits)

    server.video_held = False  # the clips finished at fal while we were down
    rt2, q2 = runtime(tmp_path, server)  # a fresh process: nothing in memory
    done = await Orchestrator(rt2).resume(run.id, stop_before=Stage.QA)
    assert done.storyboard is not None
    assert all(s.status is SceneStatus.QA_REVIEW for s in done.storyboard.scenes)
    assert q2.resumed == len(persisted)  # every persisted request was polled, not resubmitted
    assert server.submits.count("video") == 4 and server.submits.count("image") == 4
    assert len(server.submits) - len(submits_before) == 4 - submits_before.count("video")
    assert done.budget.reserved_usd == 0
