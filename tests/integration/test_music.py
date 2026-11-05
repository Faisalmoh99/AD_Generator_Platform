"""STOP 2: a generated music track for montage assembly (Lyria 2 on fal; a mock click track in
tests), cached, budgeted under a hard cap, and never a reason for a run to fail."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from adsmith.agent import commands
from adsmith.agent.recovery.errors import PermanentProviderError
from adsmith.domain.enums import Stage
from adsmith.providers.media.fal import FalMusicProvider
from adsmith.providers.media.mock import MockMusicProvider
from tests.media_helpers import fast_settings, make_harness
from tests.unit.test_fal import CDN, FakeFal, account, make_queue
from tests.unit.test_fal import settings as fal_settings

REQUEST = "Make me a 12-second ad for a luxury men's perfume for young men, luxurious and bold."


def _summaries(h: Any, run_id: str) -> list[str]:
    return [e.summary for e in h.rt.storage.events.tail(run_id, limit=10_000)]


async def _has_audio(path: Path) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_type",
        "-of", "json", str(path), stdout=asyncio.subprocess.PIPE,
    )  # fmt: skip
    out, _ = await proc.communicate()
    return bool(json.loads(out).get("streams"))


async def test_montage_gets_a_generated_track_and_is_cut_to_its_beats(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path), scenario="montage_en")
    run = h.orch.create_run(REQUEST, interactive=False)
    assert run.config.generate_music and run.config.music_path is None
    state = await h.orch.run_until_blocked(run.id)
    assert state.stage is Stage.DONE, state.error
    assert state.music is not None and state.music.kind == "audio"
    assert state.budget.by_category["audio"] == pytest.approx(0.10)
    summaries = _summaries(h, run.id)
    assert any(s.startswith("Music: generated a 30 s instrumental track") for s in summaries)
    assert any(s.startswith("Beat cut: 120 bpm") for s in summaries)
    assert state.final is not None
    assert await _has_audio(h.rt.storage.artifacts.resolve(state.final))
    music = h.rt.music_provider(state)
    assert isinstance(music, MockMusicProvider) and music.calls == 1


async def test_reassembly_after_an_edit_reuses_the_track(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path), scenario="montage_en")
    run = h.orch.create_run(REQUEST, interactive=False)
    done = await h.orch.run_until_blocked(run.id)
    commands.edit_scene_spec(h.rt.storage, done.id, 2, reason="re-prompt")
    after = await h.orch.run_until_blocked(done.id)
    assert after.stage is Stage.DONE and after.music == done.music
    music = h.rt.music_provider(after)
    assert isinstance(music, MockMusicProvider) and music.calls == 1
    assert after.budget.by_category["audio"] == pytest.approx(0.10)


async def test_a_failed_music_call_assembles_without_music(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path), scenario="montage_en")
    run = h.orch.create_run(REQUEST, interactive=False)
    music = h.rt.music_provider(run)
    assert isinstance(music, MockMusicProvider)
    music.fail_with = PermanentProviderError("fal lyria2: 422 bad prompt")
    state = await h.orch.run_until_blocked(run.id)
    assert state.stage is Stage.DONE and state.music is None
    assert state.budget.by_category.get("audio", 0.0) == 0.0
    summaries = _summaries(h, run.id)
    assert any(s.startswith("Music generation failed") for s in summaries)
    assert not any(s.startswith("Beat cut:") for s in summaries)


async def test_a_price_above_the_music_cap_is_never_called(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path, music_max_usd=0.05), scenario="montage_en")
    run = h.orch.create_run(REQUEST, interactive=False)
    state = await h.orch.run_until_blocked(run.id)
    assert state.stage is Stage.DONE and state.music is None
    music = h.rt.music_provider(state)
    assert isinstance(music, MockMusicProvider) and music.calls == 0
    assert any("above the $0.05 music cap" in s for s in _summaries(h, run.id))


async def test_narrative_runs_and_user_tracks_never_generate(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path))  # perfume_en: narrative
    run = h.orch.create_run("Make me a 15-second perfume ad", interactive=False)
    state = await h.orch.run_until_blocked(run.id)
    assert state.stage is Stage.DONE and state.music is None
    user = tmp_path / "user.wav"
    user.write_bytes(b"")
    h2 = make_harness(fast_settings(tmp_path / "b", music_path=user), scenario="montage_en")
    assert h2.orch.create_run(REQUEST, interactive=False).config.generate_music is False


async def test_lyria_payload_and_result(tmp_path: Path) -> None:
    s, acct = fal_settings(tmp_path), account(tmp_path)
    assert s.catalog.fal.audio is not None
    ep = s.catalog.fal.audio.endpoints["lyria2"]
    fake = FakeFal({"audio": {"url": f"{CDN}/music.wav"}}, polls_in_progress=1)
    queue, _ = make_queue(fake, acct)
    out = await FalMusicProvider(queue, ep).generate_music("dark pulse", 7, tmp_path, ref="r")
    body = fake.submitted[0]
    assert body["prompt"] == "dark pulse" and body["seed"] == 7
    assert "vocals" in body["negative_prompt"]
    assert fake.paths[0] == "POST https://queue.fal.run/fal-ai/lyria2"
    assert out.kind == "audio" and out.cost_usd == pytest.approx(0.10) and out.duration_s == 30
    assert acct.summary().settled_usd == pytest.approx(0.10)
