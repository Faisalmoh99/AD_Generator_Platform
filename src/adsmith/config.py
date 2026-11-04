"""Typed configuration.

Two sources, with separate jobs:
- ``Settings`` (pydantic-settings): runtime knobs and secrets from env / ``.env``.
- ``ModelCatalog`` (``config/models.toml``): model IDs, endpoints and prices. Model IDs
  and endpoints must never appear anywhere else in the codebase.
"""

from __future__ import annotations

import math
import tomllib
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from adsmith.domain.formats import FormatLibrary
from adsmith.domain.vocabulary import VocabLibrary

PROJECT_ROOT = Path(__file__).resolve().parents[2]

Effort = Literal["low", "medium", "high", "xhigh", "max"]
ProfileName = Literal["fast", "balanced", "quality"]
AspectRatio = Literal["16:9", "9:16", "1:1"]


class LLMNode(StrEnum):
    """Every node that calls an LLM. Each must have a model in every profile."""

    UNDERSTAND = "understand"
    IDEATE = "ideate"
    SELECT = "select"
    STORYBOARD = "storyboard"
    CONTINUITY = "continuity"
    PROMPT_WRITER = "prompt_writer"
    EDIT_PARSER = "edit_parser"
    CONFLICT_CHECK = "conflict_check"
    POLICY_REWRITE = "policy_rewrite"
    QA_CRITIC = "qa_critic"
    ENDCARD_COPY = "endcard_copy"


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NodeModel(_Frozen):
    model: str = Field(min_length=1)
    effort: Effort


class LLMModelSpec(_Frozen):
    """Per-model pricing (USD per 1M tokens) and capabilities."""

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_write: float = Field(ge=0)
    cache_read: float = Field(ge=0)
    server_fallback: bool = False


class OpenAIModels(_Frozen):
    default_model: str = ""
    vision_model: str = ""


class FalImageEndpoint(_Frozen):
    endpoint: str
    usd_per_megapixel: float = Field(default=0.0, ge=0)
    usd_per_image_flat: float | None = Field(default=None, ge=0)  # e.g. FLUX Kontext: per image
    round_up_megapixels: bool = True  # fal bills started megapixels on some endpoints
    # Reference-conditioned (image + prompt → image) endpoints name their input image field.
    image_field: str | None = None
    # Endpoint-specific request fields (verified against each schema), merged into the payload.
    params: dict[str, int | float | str | bool] = Field(default_factory=dict)

    def usd_for(self, width: int, height: int) -> float:
        if self.usd_per_image_flat is not None:
            return self.usd_per_image_flat
        mp = width * height / 1_000_000
        billed = math.ceil(mp - 1e-9) if self.round_up_megapixels else mp
        return round(max(billed, 0.0) * self.usd_per_megapixel, 6)

    @property
    def usd_per_image(self) -> float:
        """Price of one image up to 1 MP (our keyframe sizes); used by the mock provider."""
        return self.usd_for(1000, 1000)


class VideoProfile(_Frozen):
    """What a video model renders well and badly, injected into ideation, selection and the
    storyboard so concepts are designed around the model (M11). Each item says whether it was
    observed in our own real runs or is expected (vendor docs, general experience)."""

    strengths: list[str] = Field(min_length=1)
    weaknesses: list[str] = Field(min_length=1)
    max_single_shot_s: float = Field(gt=0, le=60)


class FalVideoEndpoint(_Frozen):
    endpoint: str
    adapter: Literal["kling", "seedance", "veo", "ltx"]
    image_field: str = "image_url"  # name of the start-image field in the request body
    usd_per_second: float = Field(ge=0)
    min_duration_s: float | None = None
    max_duration_s: float | None = None
    allowed_durations_s: list[float] | None = None
    profile: VideoProfile | None = None

    @model_validator(mode="after")
    def _duration_spec(self) -> Self:
        has_range = self.min_duration_s is not None and self.max_duration_s is not None
        if not has_range and not self.allowed_durations_s:
            raise ValueError("video endpoint needs min/max_duration_s or allowed_durations_s")
        return self


class FalImageCatalog(_Frozen):
    default: str
    # Image + prompt → image endpoint for keyframes that show the product (locked reference).
    reference: str | None = None
    # Multi-image endpoint for "composite" mode: [scene keyframe, reference] → the scene with the
    # reference product swapped in at the scene's own scale.
    composite: str | None = None
    endpoints: dict[str, FalImageEndpoint]


class FalVideoCatalog(_Frozen):
    default: str
    endpoints: dict[str, FalVideoEndpoint]


class FalCatalog(_Frozen):
    image: FalImageCatalog
    video: FalVideoCatalog


class ModelCatalog(_Frozen):
    default_profile: ProfileName
    profiles: dict[ProfileName, dict[LLMNode, NodeModel]]
    openai: OpenAIModels
    llm_models: dict[str, LLMModelSpec]
    fal: FalCatalog

    @model_validator(mode="after")
    def _complete(self) -> Self:
        for name, nodes in self.profiles.items():
            missing = set(LLMNode) - set(nodes)
            if missing:
                raise ValueError(f"profile {name!r} missing nodes: {sorted(missing)}")
            for node, nm in nodes.items():
                if nm.model not in self.llm_models:
                    raise ValueError(f"{name}.{node}: no price for model {nm.model!r}")
        if self.fal.image.default not in self.fal.image.endpoints:
            raise ValueError("fal.image.default not in endpoints")
        ref = self.fal.image.reference
        if ref is not None and (
            ref not in self.fal.image.endpoints or self.fal.image.endpoints[ref].image_field is None
        ):
            raise ValueError("fal.image.reference must name an endpoint with an image_field")
        if self.fal.video.default not in self.fal.video.endpoints:
            raise ValueError("fal.video.default not in endpoints")
        return self

    @classmethod
    def load(cls, path: Path) -> ModelCatalog:
        with path.open("rb") as fh:
            return cls.model_validate(tomllib.load(fh))


class ChaosConfig(_Frozen):
    seed: int = 7
    first_attempt_only: bool = True
    transient: float = Field(default=0.25, ge=0, le=1)
    policy: float = Field(default=0.15, ge=0, le=1)
    quality: float = Field(default=0.15, ge=0, le=1)

    @classmethod
    def load(cls, path: Path) -> ChaosConfig:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
        probs = raw.pop("probabilities", {})
        return cls.model_validate({**raw, **probs})


class Settings(BaseSettings):
    """Runtime settings. Env vars use the ``ADSMITH_`` prefix; API keys use their usual names."""

    model_config = SettingsConfigDict(
        env_prefix="ADSMITH_",
        env_file=".env",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # providers
    llm_provider: Literal["scripted", "anthropic", "openai"] = "scripted"
    media_provider: Literal["mock", "fal"] = "mock"
    qa_mode: Literal["auto", "mock", "llm"] = "auto"
    model_profile: ProfileName | None = None
    model: dict[LLMNode, str] = Field(default_factory=dict)  # per-node model overrides
    scripted_scenario: str | None = None  # default: perfume_<request language>
    fal_video_model: str | None = None
    fal_image_model: str | None = None

    # secrets (standard names, no prefix)
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    fal_key: SecretStr | None = Field(default=None, validation_alias="FAL_KEY")

    # run behaviour
    aspect_ratio: AspectRatio = "9:16"
    target_duration_s: float = Field(default=15.0, gt=0, le=60)
    budget_usd: float = Field(default=10.0, gt=0)
    max_attempts_per_scene: int = Field(default=8, ge=1)  # 3 keyframe tries + clips
    keyframe_max_tries: int = Field(default=3, ge=1, le=6)  # keyframe gate (ADR-0003)
    clip_candidates: int = Field(default=1, ge=1, le=4)  # best-of-N clips (2 with --hero)
    # QA-rejected clips regenerated per scene before it fails (0 = strictly one take per shot).
    qa_max_regenerations: int = Field(default=2, ge=0, le=5)
    # Locked product reference + reference-conditioned keyframes (ADR-0003 addendum 3).
    product_reference: bool = False
    # direct: Kontext(reference + scene prompt), the product keeps the packshot's scale;
    # composite: plain keyframe first (product at its in-scene size), then Kontext multi swaps in
    # the reference product at that size (ADR-0003 addendum 4).
    product_reference_mode: Literal["direct", "composite"] = "direct"
    max_total_attempts: int = Field(default=80, ge=1)  # 12 shots x 3 keyframe tries + clips
    qa_threshold: float = Field(default=0.7, ge=0, le=1)
    scene_concurrency: int = Field(default=3, ge=1, le=16)
    chaos: bool = False
    mock_latency_s: float = Field(default=1.5, ge=0)
    mock_fail_plan: str | None = None  # e.g. "2:clip:permanent,1:keyframe:transient*3"
    mock_debug_badge: bool = False  # burn "S3 · v2 · attempt 1" into mock clips
    media_long_side: int = Field(default=960, ge=256, le=4096)
    # assembly (M8)
    endcard_s: float = Field(default=2.0, ge=0, le=6)
    partial_mode: Literal["skip", "slate"] = "skip"
    music_path: Path | None = None  # user-supplied only; nothing is bundled
    # Hard ceiling for ALL real fal spend on this machine (prepaid balance, no auto top-up).
    # Every paid call reserves against it in the account ledger before it is submitted.
    fal_balance_usd: float | None = Field(default=None, ge=0)
    fal_poll_interval_s: float = Field(default=2.0, gt=0)
    fal_timeout_s: float = Field(default=900.0, gt=0)

    # paths
    data_dir: Path = PROJECT_ROOT / "data"
    account_db: Path = PROJECT_ROOT / "data" / "account.db"  # cross-run spend ledger
    config_dir: Path = PROJECT_ROOT / "config"
    prompts_dir: Path = PROJECT_ROOT / "prompts"

    # LLM call behaviour
    llm_timeout_s: float = Field(default=300.0, gt=0)
    llm_max_tokens: int = Field(default=16000, ge=1024)
    llm_max_repairs: int = Field(default=2, ge=0, le=5)
    llm_prompt_cache: bool = True
    anthropic_server_fallback: bool = True  # only applied to models with server_fallback=true

    # logging
    log_format: Literal["console", "json"] = "console"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @cached_property
    def catalog(self) -> ModelCatalog:
        return ModelCatalog.load(self.config_dir / "models.toml")

    @cached_property
    def formats(self) -> FormatLibrary:
        """Ad format grammars (config/formats.toml, ADR-0018)."""
        with (self.config_dir / "formats.toml").open("rb") as fh:
            return FormatLibrary.model_validate(tomllib.load(fh))

    @cached_property
    def vocabulary(self) -> VocabLibrary:
        """Shot vocabulary for montage shots (config/shot_vocabulary.toml, ADR-0018)."""
        with (self.config_dir / "shot_vocabulary.toml").open("rb") as fh:
            return VocabLibrary.model_validate(tomllib.load(fh))

    @cached_property
    def chaos_config(self) -> ChaosConfig:
        return ChaosConfig.load(self.config_dir / "chaos.toml")

    @property
    def active_profile(self) -> ProfileName:
        return self.model_profile or self.catalog.default_profile

    def node_model(self, node: LLMNode) -> NodeModel:
        """Resolve model + effort for a node: env override > active profile."""
        base = self.catalog.profiles[self.active_profile][node]
        override = self.model.get(node)
        return base if override is None else NodeModel(model=override, effort=base.effort)

    def resolved_qa_mode(self) -> Literal["mock", "llm"]:
        if self.qa_mode != "auto":
            return self.qa_mode
        # Mock placeholder frames would always fail a real vision critic.
        return "mock" if self.media_provider == "mock" else "llm"

    @model_validator(mode="after")
    def _keys_for_providers(self) -> Self:
        if self.llm_provider == "anthropic" and self.anthropic_api_key is None:
            raise ValueError("ADSMITH_LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY")
        if self.llm_provider == "openai" and self.openai_api_key is None:
            raise ValueError("ADSMITH_LLM_PROVIDER=openai requires OPENAI_API_KEY")
        if self.media_provider == "fal" and self.fal_key is None:
            raise ValueError("ADSMITH_MEDIA_PROVIDER=fal requires FAL_KEY")
        if self.media_provider == "fal" and self.fal_balance_usd is None:
            raise ValueError(
                "ADSMITH_MEDIA_PROVIDER=fal requires ADSMITH_FAL_BALANCE_USD (prepaid ceiling)"
            )
        return self
