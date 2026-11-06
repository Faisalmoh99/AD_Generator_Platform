"""Process-level wiring: settings, storage, prompts and LLM callers (one per provider/scenario)."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from adsmith.agent.prompts import PromptLibrary
from adsmith.agent.structured import StructuredCaller
from adsmith.config import FalImageEndpoint, FalVideoEndpoint, HiggsfieldVideoEndpoint, Settings
from adsmith.domain.run import RunConfig, RunState
from adsmith.providers.llm.base import LLMProvider
from adsmith.providers.llm.scripted import ScriptedLLMProvider
from adsmith.storage import ArtifactStore, Storage

if TYPE_CHECKING:
    from adsmith.agent.generation_runner import AdGenerationRunner
    from adsmith.agent.media import MediaGateway
    from adsmith.agent.scenes import SceneStageRunner
    from adsmith.providers.media.base import (
        MusicProvider,
        ReferenceVideoProvider,
        VideoCapabilities,
    )
    from adsmith.providers.media.chaos import ChaosPolicy
    from adsmith.providers.media.fal import FalQueue
    from adsmith.providers.media.higgsfield import HiggsfieldQueue, HiggsfieldVideoProvider
    from adsmith.providers.media.mock import MockMusicProvider
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
    # Whole-ad generations (multishot): tests inject a provider (e.g. Higgsfield on a fake server).
    ad_provider_factory: Callable[[RunState], ReferenceVideoProvider] | None = None
    _ad_providers: dict[str, ReferenceVideoProvider] = field(default_factory=dict)
    _account: SpendAccount | None = None
    _fal_queue: FalQueue | None = None
    _hf_account: SpendAccount | None = None
    _hf_q: HiggsfieldQueue | None = None
    _mock_music: MockMusicProvider | None = None

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
        ref_ep = (
            fal.image.endpoints[fal.image.reference]
            if reference_enabled(s, run) and fal.image.reference
            else None
        )
        # Both identity modes are always available; each shot picks one (PLAN_V2 item 3).
        comp_ep = (
            fal.image.endpoints[fal.image.composite]
            if ref_ep is not None and fal.image.composite
            else None
        )
        if run.config.media_provider == "fal":
            return self._fal_media(img_ep, vid_ep, chaos, long_side, ref_ep, comp_ep)
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
            reference=None
            if ref_ep is None
            else MockImageProvider(
                usd_per_image=ref_ep.usd_per_image,
                latency_s=s.mock_latency_s,
                sleep=self.sleep,
                fail_plan=plan,
            ),
            reference_prompt=self._reference_prompt,
            composite=None
            if comp_ep is None
            else MockImageProvider(
                usd_per_image=comp_ep.usd_per_image,
                latency_s=s.mock_latency_s,
                sleep=self.sleep,
                fail_plan=plan,
            ),
            composite_prompt=self._composite_prompt,
            relight_prompt=self._relight_prompt,
        )

    def _relight_prompt(self, product_name: str, lighting: str, note: str) -> str:
        tpl = self.prompts.get("keyframe_relight")
        return tpl.render(name=product_name, lighting=lighting, note=note).user

    def _composite_prompt(self, product_name: str, framing: str, note: str) -> str:
        tpl = self.prompts.get("keyframe_composite")
        return tpl.render(name=product_name, framing=framing, note=note).user

    def _reference_prompt(
        self, keyframe_prompt: str, product_name: str, framing: str, note: str
    ) -> str:
        tpl = self.prompts.get("keyframe_reference")
        return tpl.render(
            keyframe_prompt=keyframe_prompt, name=product_name, framing=framing, note=note
        ).user

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

    def hf_account(self) -> SpendAccount:
        """The Higgsfield wallet ledger: same SQLite file as fal, its own provider rows and its
        own ceiling (``ADSMITH_HF_BALANCE_USD``). Refuses to exist without a ceiling."""
        if self._hf_account is None:
            from adsmith.storage.account import SpendAccount  # noqa: PLC0415

            if self.settings.hf_balance_usd is None:
                raise RuntimeError(
                    "set ADSMITH_HF_BALANCE_USD (the prepaid Higgsfield balance) before real spend"
                )
            self._hf_account = SpendAccount(
                self.settings.account_db,
                balance_usd=self.settings.hf_balance_usd,
                provider="higgsfield",
            )
        return self._hf_account

    def hf_queue(self) -> HiggsfieldQueue:
        from adsmith.providers.media.higgsfield import HiggsfieldQueue  # noqa: PLC0415

        s = self.settings
        catalog = s.catalog.higgsfield
        if s.hf_key is None or catalog is None:
            raise RuntimeError("HF_KEY and a [higgsfield] catalog section are required")
        if self._hf_q is None:
            self._hf_q = HiggsfieldQueue(
                s.hf_key.get_secret_value(),
                base_url=catalog.base_url,
                account=self.hf_account(),
                poll_interval_s=s.hf_poll_interval_s,
                max_poll_interval_s=s.hf_max_poll_interval_s,
                timeout_s=s.hf_timeout_s,
                sleep=self.sleep,
                rng=self.rng,
            )
        return self._hf_q

    def higgsfield_video(self, model_key: str | None = None) -> HiggsfieldVideoProvider:
        """The Higgsfield reference-to-video provider for a catalog key (default: the setting,
        then the catalog default)."""
        from adsmith.media.frames import probe_video  # noqa: PLC0415
        from adsmith.providers.media.higgsfield import HiggsfieldVideoProvider  # noqa: PLC0415

        catalog = self.settings.catalog.higgsfield
        if catalog is None:
            raise RuntimeError("config/models.toml has no [higgsfield] section")
        key = model_key or self.settings.hf_video_model or catalog.video.default
        return HiggsfieldVideoProvider(self.hf_queue(), catalog.video.endpoints[key], probe_video)

    def ad_video_provider(self, run: RunState) -> ReferenceVideoProvider:
        """The run's reference-to-video provider (one per run): Higgsfield for real, else a mock
        that obeys the same endpoint's limits at a simulated price (ADR-0023)."""
        if run.id not in self._ad_providers:
            if self.ad_provider_factory is not None:
                self._ad_providers[run.id] = self.ad_provider_factory(run)
            elif run.config.ad_provider == "higgsfield":
                self._ad_providers[run.id] = self.higgsfield_video(run.config.ad_model)
            else:
                from adsmith.providers.media.mock import (  # noqa: PLC0415
                    MockReferenceVideoProvider,
                )

                ep = ad_endpoint(self.settings, run.config.ad_model)
                self._ad_providers[run.id] = MockReferenceVideoProvider(
                    model=f"mock:{ep.endpoint}",
                    usd_per_second=ep.mock_usd_per_second,
                    max_prompt_chars=ep.max_prompt_chars,
                    latency_s=self.settings.mock_latency_s,
                    sleep=self.sleep,
                    long_side=run.config.media_long_side or self.settings.media_long_side,
                    pricing=ep.pricing,
                )
        return self._ad_providers[run.id]

    def media_remaining_usd(self, run_config: RunConfig) -> float | None:
        """What is left of the prepaid wallets this run's paid media draws on (None = no real
        media provider: only the run budget applies)."""
        left: float | None = None
        if run_config.media_provider == "fal":
            left = self.fal_account().summary().remaining_usd
        if run_config.ad_provider == "higgsfield":
            left = (left or 0.0) + self.hf_account().summary().remaining_usd
        return left

    def _fal_media(
        self,
        img_ep: FalImageEndpoint,
        vid_ep: FalVideoEndpoint,
        chaos: ChaosPolicy,
        long_side: int,
        ref_ep: FalImageEndpoint | None = None,
        comp_ep: FalImageEndpoint | None = None,
    ) -> MediaGateway:
        from adsmith.agent.media import MediaGateway  # noqa: PLC0415
        from adsmith.media.frames import probe_video  # noqa: PLC0415
        from adsmith.providers.adapters.base import ADAPTERS  # noqa: PLC0415
        from adsmith.providers.media.fal import (  # noqa: PLC0415
            FalImageProvider,
            FalVideoProvider,
        )

        queue = self._queue()
        return MediaGateway(
            image=FalImageProvider(queue, img_ep),
            video=FalVideoProvider(queue, vid_ep, video_capabilities(vid_ep), probe_video),
            adapter=ADAPTERS[vid_ep.adapter],
            reference=None if ref_ep is None else FalImageProvider(queue, ref_ep),
            reference_prompt=self._reference_prompt,
            composite=None if comp_ep is None else FalImageProvider(queue, comp_ep),
            composite_prompt=self._composite_prompt,
            relight_prompt=self._relight_prompt,
            storage=self.storage,
            chaos=chaos,
            long_side=long_side,
        )

    def _queue(self) -> FalQueue:
        from adsmith.providers.media.fal import FalQueue  # noqa: PLC0415

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
        return self._fal_queue

    def music_provider(self, run: RunState) -> MusicProvider | None:
        """The run's text-to-music provider (STOP 2), or None if the catalog has none. Mock media
        (and tests with an injected media factory) get the deterministic mock track."""
        audio = self.settings.catalog.fal.audio
        if audio is None:
            return None
        ep = audio.endpoints[audio.default]
        if run.config.media_provider == "fal" and self.media_factory is None:
            from adsmith.providers.media.fal import FalMusicProvider  # noqa: PLC0415

            return FalMusicProvider(self._queue(), ep)
        from adsmith.providers.media.mock import MockMusicProvider  # noqa: PLC0415

        if self._mock_music is None:
            self._mock_music = MockMusicProvider(
                usd_per_track=ep.usd_per_track, track_s=ep.track_s, sleep=self.sleep
            )
        return self._mock_music

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

    def ad_runner(self, run: RunState) -> AdGenerationRunner:
        from adsmith.agent.generation_runner import AdGenerationRunner  # noqa: PLC0415
        from adsmith.agent.qa import LLMCritic, MockCritic  # noqa: PLC0415

        critic = LLMCritic(self.node_context(run)) if run.config.qa_mode == "llm" else MockCritic()
        return AdGenerationRunner(
            storage=self.storage,
            provider=self.ad_video_provider(run),
            critic=critic,
            sleep=self.sleep,
            rng=self.rng,
        )

    def _make_provider(self, run: RunState) -> LLMProvider:
        if self._provider_override is not None:
            return self._provider_override
        provider = run.config.llm_provider
        if provider == "scripted":
            return ScriptedLLMProvider.from_scenario(
                run.config.scripted_scenario or "perfume_en",
                latency_s=self.settings.scripted_latency_s,
                sleep=self.sleep,
            )
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


def reference_enabled(settings: Settings, run: RunState) -> bool:
    """The locked product reference runs when asked for (``--product-ref``) or when the run's
    format requires it (montage: identity = "reference" in formats.toml)."""
    if run.config.product_reference:
        return True
    sb = run.storyboard
    return sb is not None and settings.formats.grammar(sb.format).identity == "reference"


def ad_model_key(settings: Settings, ad_model: str | None) -> str:
    """Catalog key of the reference-to-video endpoint: the run's, else the setting's, else the
    catalog default."""
    catalog = settings.catalog.higgsfield
    if catalog is None:
        raise RuntimeError("config/models.toml has no [higgsfield] section")
    return ad_model or settings.hf_video_model or catalog.video.default


def ad_endpoint(settings: Settings, ad_model: str | None) -> HiggsfieldVideoEndpoint:
    """The reference-to-video endpoint whose limits a whole-ad run plans for (mock runs too)."""
    catalog = settings.catalog.higgsfield
    assert catalog is not None
    return catalog.video.endpoints[ad_model_key(settings, ad_model)]


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
