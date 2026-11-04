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

from collections.abc import Callable
from dataclasses import dataclass, replace
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
    MediaOutput,
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
    # Composite mode: after the plain keyframe (job.prompt), swap the reference in with this prompt.
    composite_prompt: str | None = None


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
        reference: ImageProvider | None = None,
        reference_prompt: Callable[[str, str], str] | None = None,
        composite: ImageProvider | None = None,
        composite_prompt: Callable[[str], str] | None = None,
    ) -> None:
        self.image, self.video, self.adapter = image, video, adapter
        self.storage, self.chaos, self.long_side = storage, chaos, long_side
        # Image + prompt → image provider for keyframes that show the product, and the wrapper
        # that tells it to keep the reference product unchanged (ADR-0003 addendum 3).
        self.reference, self.reference_prompt = reference, reference_prompt
        # Composite mode (ADR-0003 addendum 4): plain keyframe first, then a multi-image model
        # swaps the reference product in at the scene's own scale.
        self.composite, self.composite_prompt = composite, composite_prompt

    # ------------------------------------------------------------------------ preparation
    def prepare(
        self,
        scene: Scene,
        phase: ScenePhase,
        *,
        aspect_ratio: str,
        palette: tuple[str, ...],
        attempt_no: int,
        product_reference: Artifact | None = None,
        product_name: str = "product",
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
            plain = self._prepare_image(scene.prompt, aspect_ratio, label)
            if product_reference is not None and scene.shows_product:
                return self._condition_on(plain, product_reference, product_name)
            return plain
        assert scene.keyframe is not None
        return self._prepare_video(scene, scene.prompt, aspect_ratio, label)

    def _prepare_image(self, prompt: ScenePrompt, aspect: str, label: MediaLabel) -> PreparedJob:
        w, h = frame_size(aspect, long_side=self.long_side)
        # First attempt: seed 0, so the same prompt gives the same keyframe (and cache key). A
        # retry (e.g. after QA rejected the look) gets a new seed, so even an unchanged prompt
        # yields a different image instead of the rejected one.
        seed = label.attempt_no - 1
        request = GenerationRequest(
            provider=self.image.name,
            model=self.image.model,
            phase=ScenePhase.KEYFRAME,
            prompt=prompt.keyframe_prompt,
            negative_prompt=prompt.negative_prompt,
            params={"width": w, "height": h, "seed": seed},
        )
        job = ImageJob(prompt.keyframe_prompt, prompt.negative_prompt, w, h, seed, label)
        return PreparedJob(request, job, self.image.estimate_usd(w, h))

    def prepare_product_reference(self, prompt: str, aspect: str, attempt_no: int) -> PreparedJob:
        """The run's locked product still: text-to-image with the default image model. Label
        scene 0, so traces and fal refs read "s0" (no scene owns it)."""
        label = MediaLabel(
            scene_order=0, scene_version=1, phase=ScenePhase.KEYFRAME, attempt_no=attempt_no,
            shot="product", camera="static", palette=(),
        )  # fmt: skip
        still = ScenePrompt(
            keyframe_prompt=prompt, motion_prompt="(still)", template_version="product_reference",
            bible_version=1,
        )  # fmt: skip
        return self._prepare_image(still, aspect, label)

    def _condition_on(
        self, plain: PreparedJob, reference: Artifact, product_name: str
    ) -> PreparedJob:
        """Same keyframe, generated from the locked product still by the reference model. The
        reference's sha256 is part of the request, so a new reference is a new cache key."""
        if self.composite is not None and self.composite_prompt is not None:
            return self._composite(plain, reference, product_name)
        if self.reference is None or self.reference_prompt is None:
            return plain
        assert isinstance(plain.job, ImageJob)
        prompt = self.reference_prompt(plain.job.prompt, product_name)
        request = plain.request.model_copy(
            update={
                "provider": self.reference.name,
                "model": self.reference.model,
                "prompt": prompt,
                "input_image_sha256": reference.sha256,
            }
        )
        job = replace(
            plain.job, prompt=prompt, reference_path=self.storage.artifacts.resolve(reference)
        )
        return PreparedJob(request, job, self.reference.estimate_usd(job.width, job.height))

    def _composite(self, plain: PreparedJob, reference: Artifact, name: str) -> PreparedJob:
        assert isinstance(plain.job, ImageJob) and self.composite and self.composite_prompt
        swap = self.composite_prompt(name)
        request = plain.request.model_copy(
            update={
                "provider": self.composite.name,
                "model": f"{self.image.model}+{self.composite.model}",
                "prompt": f"{plain.request.prompt}\n---\n{swap}",
                "input_image_sha256": reference.sha256,
            }
        )
        job = replace(plain.job, reference_path=self.storage.artifacts.resolve(reference))
        est = plain.est_usd + self.composite.estimate_usd(job.width, job.height)
        return PreparedJob(request, job, round(est, 6), composite_prompt=swap)

    async def _run_composite(self, prepared: PreparedJob, scratch: Path) -> MediaOutput:
        """Plain keyframe (archived in the CAS for the record), then the multi-image swap."""
        assert isinstance(prepared.job, ImageJob) and self.composite is not None
        assert prepared.composite_prompt is not None
        plain_job = replace(prepared.job, reference_path=None)
        plain = await self.image.generate_image(plain_job, scratch)
        base = self.storage.artifacts.ingest(
            plain.path, kind="image", provider=self.image.name, width=plain.width,
            height=plain.height, remote_url=plain.remote_url, move=True,
        )  # fmt: skip
        comp_job = replace(
            prepared.job,
            prompt=prepared.composite_prompt,
            base_path=self.storage.artifacts.resolve(base),
        )
        out = await self.composite.generate_image(comp_job, scratch)
        return replace(out, cost_usd=round(out.cost_usd + plain.cost_usd, 6))

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
        if prepared.composite_prompt is not None:
            out = await self._run_composite(prepared, scratch)
            provider = self.composite.name if self.composite else self.image.name
        elif isinstance(job, ImageJob) and job.reference_path is not None:
            assert self.reference is not None
            out = await self.reference.generate_image(job, scratch)
            provider = self.reference.name
        elif isinstance(job, ImageJob):
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
