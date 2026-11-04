"""Typed configuration.

Two sources, with separate jobs:
- ``Settings`` (pydantic-settings): runtime knobs and secrets from env / ``.env``.
- ``ModelCatalog`` (``config/models.toml``): model IDs, endpoints and prices. Model IDs
  and endpoints must never appear anywhere else in the codebase.
"""

from __future__ import annotations

import tomllib
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NodeModel(_Frozen):
    model: str = Field(min_length=1)
    effort: Effort


class TokenPrice(_Frozen):
    input: float = Field(ge=0)
    output: float = Field(ge=0)


class OpenAIModels(_Frozen):
    default_model: str = ""
    vision_model: str = ""


class FalImageEndpoint(_Frozen):
    endpoint: str
    usd_per_image: float = Field(ge=0)


class FalVideoEndpoint(_Frozen):
    endpoint: str
    adapter: Literal["kling", "seedance", "veo"]
    usd_per_second: float = Field(ge=0)
    min_duration_s: float | None = None
    max_duration_s: float | None = None
    allowed_durations_s: list[float] | None = None

    @model_validator(mode="after")
    def _duration_spec(self) -> Self:
        has_range = self.min_duration_s is not None and self.max_duration_s is not None
        if not has_range and not self.allowed_durations_s:
            raise ValueError("video endpoint needs min/max_duration_s or allowed_durations_s")
        return self


class FalImageCatalog(_Frozen):
    default: str
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
    llm_prices: dict[str, TokenPrice]
    fal: FalCatalog

    @model_validator(mode="after")
    def _complete(self) -> Self:
        for name, nodes in self.profiles.items():
            missing = set(LLMNode) - set(nodes)
            if missing:
                raise ValueError(f"profile {name!r} missing nodes: {sorted(missing)}")
            for node, nm in nodes.items():
                if nm.model not in self.llm_prices:
                    raise ValueError(f"{name}.{node}: no price for model {nm.model!r}")
        if self.fal.image.default not in self.fal.image.endpoints:
            raise ValueError("fal.image.default not in endpoints")
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
    max_attempts_per_scene: int = Field(default=6, ge=1)
    max_total_attempts: int = Field(default=40, ge=1)
    qa_threshold: float = Field(default=0.7, ge=0, le=1)
    scene_concurrency: int = Field(default=3, ge=1, le=16)
    chaos: bool = False
    mock_latency_s: float = Field(default=1.5, ge=0)

    # paths
    data_dir: Path = PROJECT_ROOT / "data"
    config_dir: Path = PROJECT_ROOT / "config"
    prompts_dir: Path = PROJECT_ROOT / "prompts"

    # logging
    log_format: Literal["console", "json"] = "console"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @cached_property
    def catalog(self) -> ModelCatalog:
        return ModelCatalog.load(self.config_dir / "models.toml")

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
        return self
