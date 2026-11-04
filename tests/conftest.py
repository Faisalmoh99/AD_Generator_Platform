"""Shared fixtures. Unit tests never touch the network or real API keys."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from adsmith.config import Settings

_KEY_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "FAL_KEY")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--live", action="store_true", help="run tests that hit real APIs")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--live"):
        return
    skip_live = pytest.mark.skip(reason="needs --live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(autouse=True)
def _isolated_env(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    """Strip real keys and ADSMITH_* vars so tests are hermetic regardless of the dev's shell.
    Live tests (opt-in with --live) keep the real keys."""
    if request.node.get_closest_marker("live") is not None:
        yield
        return
    for var in list(os.environ):
        if var.startswith("ADSMITH_") or var in _KEY_VARS:
            monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)  # no stray .env is picked up
    yield


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", mock_latency_s=0.0, media_long_side=320)
