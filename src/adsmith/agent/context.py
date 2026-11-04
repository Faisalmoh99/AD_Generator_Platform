"""Process-level wiring: settings, storage, prompts and LLM callers (one per provider/scenario)."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from adsmith.agent.prompts import PromptLibrary
from adsmith.agent.structured import StructuredCaller
from adsmith.config import FalImageEndpoint, FalVideoEndpoint, Settings
from adsmith.domain.run import RunState
from adsmith.providers.llm.base import LLMProvider
from adsmith.providers.llm.scripted import ScriptedLLMProvider
from adsmith.storage import ArtifactStore, Storage

if TYPE_CHECKING:
    from adsmith.agent.media import MediaGateway
    from adsmith.agent.scenes import SceneStageRunner
    from adsmith.providers.media.base import VideoCapabilities
    from adsmith.providers.media.chaos import ChaosPolicy
    from adsmith.providers.media.fal import FalQueue
    from adsmith.storage.account import SpendAccount


@dataclass(frozen=True, slots=True)
class NodeContext:
    """What a node may use. Note what is absent: storage. Nodes cannot write state."""

    run_id: str
    settings: Settings
    prompts: PromptLibrary
    llm: StructuredCaller
    artifacts: ArtifactStore  # file store for media I/O (not run state)


@dataclass
class AgentRuntime:
    settings: Settings
    storage: Storage
    prompts: PromptLibrary
    rng: random.Random = field(default_factory=random.Random)
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep  # injected: tests don't wait
    _callers: dict[str, StructuredCaller] = field(default_factory=dict)
    _provider_override: LLMProvider | None = None
    _gateways: dict[str, MediaGateway] = field(default_factory=dict)
    media_factory: Callable[[RunState], MediaGateway] | None = None  # tests / demos
    _account: SpendAccount | None = None
    _fal_queue: FalQueue | None = None

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        llm_provider: LLMProvider | None = None,
        media_factory: Callable[[RunState], MediaGateway] | None = None,
    ) -> AgentRuntime:
        return cls(
            settings=settings,
            storage=Storage.open(settings.data_dir),
            prompts=PromptLibrary(settings.prompts_dir),
            _provider_override=llm_provider,
            media_factory=media_factory,
        )

    # ------------------------------------------------------------------ media (one gateway per run)
    def media_gateway(self, run: RunState) -> MediaGateway:
        if run.id not in self._gateways:
            factory = self.media_factory or self._default_media
            self._gateways[run.id] = factory(run)
        return self._gateways[run.id]

    def _default_media(self, run: RunState) -> MediaGateway:
        from adsmith.agent.media import MediaGateway  # noqa: PLC0415
        from adsmith.providers.adapters.base import ADAPTERS  # noqa: PLC0415
        from adsmith.providers.media.chaos import ChaosPolicy  # noqa: PLC0415
        from adsmith.providers.media.mock import (  # noqa: PLC0415
            FailPlan,
            MockImageProvider,
            MockVideoProvider,
            parse_fail_plan,
        )

        s = self.settings
        fal = s.catalog.fal
        img_ep = fal.image.endpoints[
            run.config.image_model or s.fal_image_model or fal.image.default
        ]
        vid_ep = fal.video.endpoints[
            run.config.video_model or s.fal_video_model or fal.video.default
        ]
        long_side = run.config.media_long_side or s.media_long_side
        chaos = ChaosPolicy(s.chaos_config, enabled=run.config.chaos)
        if run.config.media_provider == "fal":
            return self._fal_media(img_ep, vid_ep, chaos, long_side)
        plan = FailPlan(parse_fail_plan(s.mock_fail_plan))
        # Mock media is priced at the real endpoints' rates and obeys their duration rules, so
        # the budget guard and duration snapping behave exactly as they will with fal.
        return MediaGateway(
            image=MockImageProvider(
                usd_per_image=img_ep.usd_per_image,
                latency_s=s.mock_latency_s,
                sleep=self.sleep,
                fail_plan=plan,
            ),
            video=MockVideoProvider(
                usd_per_second=vid_ep.usd_per_second,
                capabilities=video_capabilities(vid_ep),
                latency_s=s.mock_latency_s,
                sleep=self.sleep,
                fail_plan=plan,
                debug_badge=s.mock_debug_badge,
            ),
            adapter=ADAPTERS["mock"],
            storage=self.storage,
            chaos=chaos,
            long_side=long_side,
        )

    def fal_account(self) -> SpendAccount:
        """The cross-run wallet ledger (one per process). Refuses to exist without a ceiling."""
        if self._account is None:
            from adsmith.storage.account import SpendAccount  # noqa: PLC0415

            if self.settings.fal_balance_usd is None:
                raise RuntimeError(
                    "set ADSMITH_FAL_BALANCE_USD (the prepaid fal balance) before real media spend"
                )
            self._account = SpendAccount(
                self.settings.account_db, balance_usd=self.settings.fal_balance_usd
            )
        return self._account

    def _fal_media(
        self,
        img_ep: FalImageEndpoint,
        vid_ep: FalVideoEndpoint,
        chaos: ChaosPolicy,
        long_side: int,
    ) -> MediaGateway:
        from adsmith.agent.media import MediaGateway  # noqa: PLC0415
        from adsmith.media.frames import probe_video  # noqa: PLC0415
        from adsmith.providers.adapters.base import ADAPTERS  # noqa: PLC0415
        from adsmith.providers.media.fal import (  # noqa: PLC0415
            FalImageProvider,
            FalQueue,
            FalVideoProvider,
        )

        s = self.settings
        if s.fal_key is None:
            raise RuntimeError("FAL_KEY is required for the fal media provider")
        if self._fal_queue is None:
            self._fal_queue = FalQueue(
                s.fal_key.get_secret_value(),
                account=self.fal_account(),
                poll_interval_s=s.fal_poll_interval_s,
                timeout_s=s.fal_timeout_s,
                sleep=self.sleep,
            )
        return MediaGateway(
            image=FalImageProvider(self._fal_queue, img_ep),
            video=FalVideoProvider(
                self._fal_queue, vid_ep, video_capabilities(vid_ep), probe_video
            ),
            adapter=ADAPTERS[vid_ep.adapter],
            storage=self.storage,
            chaos=chaos,
            long_side=long_side,
        )

    def scene_runner(self, run: RunState) -> SceneStageRunner:
        from adsmith.agent.qa import LLMCritic, MockCritic  # noqa: PLC0415
        from adsmith.agent.scenes import SceneStageRunner  # noqa: PLC0415

        node_ctx = self.node_context(run)
        critic = LLMCritic(node_ctx) if run.config.qa_mode == "llm" else MockCritic()
        return SceneStageRunner(
            storage=self.storage,
            gateway=self.media_gateway(run),
            node_ctx=node_ctx,
            critic=critic,
            concurrency=self.settings.scene_concurrency,
            sleep=self.sleep,
            rng=self.rng,
        )

    def _make_provider(self, run: RunState) -> LLMProvider:
        if self._provider_override is not None:
            return self._provider_override
        provider = run.config.llm_provider
        if provider == "scripted":
            return ScriptedLLMProvider.from_scenario(run.config.scripted_scenario or "perfume_en")
        if provider == "anthropic":
            from adsmith.providers.llm.anthropic import AnthropicProvider  # noqa: PLC0415

            key = self.settings.anthropic_api_key
            if key is None:
                raise RuntimeError("ANTHROPIC_API_KEY is required for the anthropic provider")
            return AnthropicProvider(key.get_secret_value(), timeout_s=self.settings.llm_timeout_s)
        from adsmith.providers.llm.openai import OpenAIProvider  # noqa: PLC0415

        okey = self.settings.openai_api_key
        if okey is None:
            raise RuntimeError("OPENAI_API_KEY is required for the openai provider")
        return OpenAIProvider(okey.get_secret_value(), timeout_s=self.settings.llm_timeout_s)

    def node_context(self, run: RunState) -> NodeContext:
        key = f"{run.config.llm_provider}:{run.config.scripted_scenario}"
        if key not in self._callers:
            self._callers[key] = StructuredCaller(
                provider=self._make_provider(run),
                settings=self.settings,
                prompts=self.prompts,
                sleep=self.sleep,
                rng=self.rng,
            )
        return NodeContext(
            run_id=run.id,
            settings=self.settings,
            prompts=self.prompts,
            llm=self._callers[key],
            artifacts=self.storage.artifacts,
        )


def video_capabilities(endpoint: FalVideoEndpoint) -> VideoCapabilities:
    from adsmith.providers.media.base import VideoCapabilities as Caps  # noqa: PLC0415

    allowed = tuple(endpoint.allowed_durations_s) if endpoint.allowed_durations_s else None
    return Caps(
        min_duration_s=endpoint.min_duration_s or min(allowed or (1.0,)),
        max_duration_s=endpoint.max_duration_s or max(allowed or (60.0,)),
        allowed_durations_s=allowed,
        supports_negative=endpoint.adapter not in {"seedance", "ltx"},
    )


def render_window(settings: Settings, run: RunState, duration_s: float) -> tuple[float, float]:
    """(rendered, visible) seconds for a scene on this run's video model. Models snap durations
    upwards (LTX-2.3 Fast renders ≥6 s) and assembly cuts the tail, so prompts must plan motion
    for the visible window (ADR-0017)."""
    fal = settings.catalog.fal.video
    ep = fal.endpoints[run.config.video_model or settings.fal_video_model or fal.default]
    return video_capabilities(ep).snap(duration_s), duration_s
