from __future__ import annotations

import pytest
from pydantic import ValidationError

from adsmith.config import LLMNode, Settings


def test_defaults_are_zero_key(settings: Settings) -> None:
    assert settings.llm_provider == "scripted"
    assert settings.media_provider == "mock"
    assert settings.aspect_ratio == "9:16"
    assert settings.budget_usd == 10.0
    assert settings.resolved_qa_mode() == "mock"


def test_catalog_loads_and_every_profile_covers_every_node(settings: Settings) -> None:
    catalog = settings.catalog
    for profile in ("fast", "balanced", "quality"):
        assert set(catalog.profiles[profile]) == set(LLMNode)


def test_balanced_profile_uses_haiku_for_narrow_nodes(settings: Settings) -> None:
    assert settings.active_profile == "balanced"
    assert settings.node_model(LLMNode.UNDERSTAND).model == "claude-haiku-5-5"
    assert settings.node_model(LLMNode.EDIT_PARSER).model == "claude-haiku-5-5"
    assert settings.node_model(LLMNode.STORYBOARD).model == "claude-sonnet-5-5"


def test_per_node_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADSMITH_MODEL__STORYBOARD", "claude-opus-5-5")
    s = Settings()
    nm = s.node_model(LLMNode.STORYBOARD)
    assert nm.model == "claude-opus-5-5"
    assert nm.effort == s.catalog.profiles["balanced"][LLMNode.STORYBOARD].effort


def test_quality_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADSMITH_MODEL_PROFILE", "quality")
    assert Settings().node_model(LLMNode.IDEATE).model == "claude-opus-5-5"


def test_real_provider_without_key_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADSMITH_LLM_PROVIDER", "anthropic")
    with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY"):
        Settings()


def test_fal_default_video_model_is_kling_v3_pro(settings: Settings) -> None:
    video = settings.catalog.fal.video
    assert video.endpoints[video.default].adapter == "kling"


def test_chaos_config_loads(settings: Settings) -> None:
    cc = settings.chaos_config
    assert cc.first_attempt_only is True
    assert 0 <= cc.transient <= 1
