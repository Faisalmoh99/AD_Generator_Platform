"""MediaGateway: one generation call = cache → chaos → provider → CAS → cache (ADR-0006).

Order matters:
- **Cache first**: a hit returns the stored artifact at zero cost and cannot be faulted by chaos.
- **Chaos** injects transient/policy faults *instead of* calling the provider (like a real
  rejection), and marks quality faults on a real output so the QA stage can reject it.
- **Successes are cached; failures and known-bad (quality-faulted) outputs never are.**
Budget reservation and the retry/recovery policy live one level up (``agent/scenes.py``), because
they change run state; the gateway only executes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from adsmith.agent.recovery.errors import ContentPolicyError, TransientProviderError
from adsmith.domain.enums import FailureKind, ScenePhase
from adsmith.domain.generation import Artifact, GenerationRequest
from adsmith.domain.scene import Scene, ScenePrompt
from adsmith.providers.adapters.base import PromptAdapter
from adsmith.providers.media.base import (
    ImageJob,
    ImageProvider,
    MediaLabel,
    VideoJob,
    VideoProvider,
    frame_size,
)
from adsmith.providers.media.chaos import ChaosPolicy
from adsmith.storage import Storage


@dataclass(frozen=True, slots=True)
class PreparedJob:
    request: GenerationRequest  # its cache_key() is the idempotency key
    job: ImageJob | VideoJob
    est_usd: float


@dataclass(frozen=True, slots=True)
class GatewayResult:
    artifact: Artifact
    cache_hit: bool
    cost_usd: float
    injected_fault: FailureKind | None = None


class MediaGateway:
    def __init__(
        self,
        *,
        image: ImageProvider,
        video: VideoProvider,
        adapter: PromptAdapter,
        storage: Storage,
        chaos: ChaosPolicy,
        long_side: int,
    ) -> None:
        self.image, self.video, self.adapter = image, video, adapter
        self.storage, self.chaos, self.long_side = storage, chaos, long_side

    # ------------------------------------------------------------------------ preparation
    def prepare(
        self,
        scene: Scene,
        phase: ScenePhase,
        *,
        aspect_ratio: str,
        palette: tuple[str, ...],
        attempt_no: int,
    ) -> PreparedJob:
        assert scene.prompt is not None
        label = MediaLabel(
            scene_order=scene.order,
            scene_version=scene.version,
            phase=phase,
            attempt_no=attempt_no,
            shot=scene.shot.shot_type.value,
            camera=scene.shot.camera_movement.value,
            palette=palette,
        )
        if phase is ScenePhase.KEYFRAME:
            return self._prepare_image(scene.prompt, aspect_ratio, label)
        assert scene.keyframe is not None
        return self._prepare_video(scene, scene.prompt, aspect_ratio, label)

    def _prepare_image(self, prompt: ScenePrompt, aspect: str, label: MediaLabel) -> PreparedJob:
        w, h = frame_size(aspect, long_side=self.long_side)
        seed = 0  # fixed: same prompt → same keyframe (and same cache key)
        request = GenerationRequest(
            provider=self.image.name,
            model=self.image.model,
            phase=ScenePhase.KEYFRAME,
            prompt=prompt.keyframe_prompt,
            negative_prompt=prompt.negative_prompt,
            params={"width": w, "height": h, "seed": seed},
        )
        job = ImageJob(prompt.keyframe_prompt, prompt.negative_prompt, w, h, seed, label)
        return PreparedJob(request, job, self.image.usd_per_image)

    def _prepare_video(
        self, scene: Scene, prompt: ScenePrompt, aspect: str, label: MediaLabel
    ) -> PreparedJob:
        assert scene.keyframe is not None
        caps = self.video.capabilities
        adapted = self.adapter.adapt(prompt, scene.duration_s, aspect, caps)
        w, h = frame_size(aspect, long_side=self.long_side)
        request = GenerationRequest(
            provider=self.video.name,
            model=self.video.model,
            phase=ScenePhase.CLIP,
            prompt=adapted.prompt,
            negative_prompt=adapted.negative_prompt,
            params={**adapted.params, "width": w, "height": h, "adapter": self.adapter.name},
            input_image_sha256=scene.keyframe.sha256,
        )
        job = VideoJob(
            image_path=self.storage.artifacts.resolve(scene.keyframe),
            prompt=adapted.prompt,
            negative_prompt=adapted.negative_prompt,
            duration_s=adapted.duration_s,
            width=w,
            height=h,
            params=adapted.params,
            label=label,
        )
        return PreparedJob(request, job, round(self.video.usd_per_second * adapted.duration_s, 6))

    # ------------------------------------------------------------------------ execution
    def cached(self, prepared: PreparedJob) -> Artifact | None:
        return self.storage.cache.get(prepared.request.cache_key())

    async def run(
        self, prepared: PreparedJob, scratch: Path, *, transport_try: int = 1
    ) -> GatewayResult:
        key = prepared.request.cache_key()
        hit = self.storage.cache.get(key)
        if hit is not None:
            return GatewayResult(artifact=hit, cache_hit=True, cost_usd=0.0)

        fault = self.chaos.decide(prepared.job.label, transport_try=transport_try)
        if fault is FailureKind.TRANSIENT:
            err: TransientProviderError | ContentPolicyError = TransientProviderError(
                "chaos: simulated 503 from the provider"
            )
            err.injected = True
            raise err
        if fault is FailureKind.CONTENT_POLICY:
            err = ContentPolicyError(
                "chaos: simulated content-policy rejection of the prompt", category="chaos"
            )
            err.injected = True
            raise err

        job = prepared.job
        if isinstance(job, ImageJob):
            out = await self.image.generate_image(job, scratch)
            provider = self.image.name
        else:
            out = await self.video.image_to_video(job, scratch)
            provider = self.video.name
        artifact = self.storage.artifacts.ingest(
            out.path,
            kind=out.kind,
            provider=provider,
            width=out.width,
            height=out.height,
            duration_s=out.duration_s,
            remote_url=out.remote_url,
            move=True,
        )
        if fault is not FailureKind.QUALITY_REJECTED:
            self.storage.cache.put(key, artifact)
        return GatewayResult(
            artifact=artifact, cache_hit=False, cost_usd=out.cost_usd, injected_fault=fault
        )
