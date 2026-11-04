"""Harness for scene-pipeline tests: scripted LLM + real mock media (Pillow/ffmpeg), no waiting."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from adsmith.agent.context import AgentRuntime, video_capabilities
from adsmith.agent.media import MediaGateway
from adsmith.agent.orchestrator import Orchestrator
from adsmith.config import ChaosConfig, Settings
from adsmith.domain.run import RunState
from adsmith.providers.adapters.base import ADAPTERS
from adsmith.providers.llm.scripted import ScriptedLLMProvider
from adsmith.providers.media.chaos import ChaosPolicy
from adsmith.providers.media.mock import (
    FailPlan,
    MockImageProvider,
    MockVideoProvider,
    parse_fail_plan,
)

REQUEST = "Make me a 15-second ad for a luxury men's perfume targeting young men, cinematic."


def fast_settings(tmp_path: Path, **over: object) -> Settings:
    base: dict[str, object] = {
        "data_dir": tmp_path / "data",
        "mock_latency_s": 0.0,
        "media_long_side": 320,
    }
    return Settings(**(base | over))  # type: ignore[arg-type]


@dataclass
class Harness:
    orch: Orchestrator
    rt: AgentRuntime
    image: MockImageProvider
    video: MockVideoProvider
    sleeps: list[float] = field(default_factory=list)
    llm: ScriptedLLMProvider | None = None
    reference: MockImageProvider | None = None
    composite: MockImageProvider | None = None

    @property
    def provider_calls(self) -> int:
        return self.image.calls + self.video.calls


def make_harness(
    settings: Settings,
    *,
    fail_plan: str = "",
    chaos: ChaosConfig | None = None,
    scenario: str = "perfume_en",
    scripts: dict[str, list[dict[str, Any]]] | None = None,
) -> Harness:
    llm = ScriptedLLMProvider.from_scenario(scenario)
    for template, steps in (scripts or {}).items():
        llm.override(template, steps)
    rt = AgentRuntime.from_settings(settings, llm_provider=llm)
    sleeps: list[float] = []

    async def no_wait(seconds: float) -> None:
        sleeps.append(seconds)

    rt.sleep = no_wait
    plan = FailPlan(parse_fail_plan(fail_plan))
    catalog = settings.catalog.fal
    image = MockImageProvider(
        usd_per_image=catalog.image.endpoints[catalog.image.default].usd_per_image,
        latency_s=0,
        sleep=no_wait,
        fail_plan=plan,
    )
    vid_ep = catalog.video.endpoints[catalog.video.default]
    video = MockVideoProvider(
        usd_per_second=vid_ep.usd_per_second,
        capabilities=video_capabilities(vid_ep),
        latency_s=0,
        sleep=no_wait,
        fail_plan=plan,
    )
    policy = ChaosPolicy(chaos or settings.chaos_config, enabled=chaos is not None)

    reference = MockImageProvider(usd_per_image=0.04, latency_s=0, sleep=no_wait, fail_plan=plan)
    reference.model = "mock-reference"
    composite = MockImageProvider(usd_per_image=0.04, latency_s=0, sleep=no_wait, fail_plan=plan)
    composite.model = "mock-composite"
    composite_on = settings.product_reference_mode == "composite"

    def factory(run: RunState) -> MediaGateway:
        return MediaGateway(
            image=image,
            video=video,
            adapter=ADAPTERS["mock"],
            storage=rt.storage,
            chaos=policy,
            long_side=settings.media_long_side,
            reference=reference if run.config.product_reference else None,
            reference_prompt=rt._reference_prompt,
            composite=composite if run.config.product_reference and composite_on else None,
            composite_prompt=rt._composite_prompt,
        )

    rt.media_factory = factory
    return Harness(Orchestrator(rt), rt, image, video, sleeps, llm, reference, composite)
