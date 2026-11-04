"""M8: assembly with an end card (Arabic shaped), placeholder brand, partial mode, music,
duration check."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from adsmith.agent.edit.service import EditService
from adsmith.domain.endcard import real_brand_in
from adsmith.domain.enums import Stage
from adsmith.domain.events import EventType
from adsmith.domain.run import RunState
from adsmith.media.endcard import font, missing_glyphs, shape
from adsmith.providers.llm.scripted import FIXTURES_DIR
from tests.media_helpers import REQUEST, Harness, fast_settings, make_harness

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def probe(path: Path) -> dict[str, Any]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    data: dict[str, Any] = json.loads(out.stdout)
    return data


async def run(h: Harness) -> RunState:
    state = h.orch.create_run(REQUEST, interactive=False)
    return await h.orch.run_until_blocked(state.id)


def final_path(h: Harness, state: RunState) -> Path:
    assert state.final is not None
    return h.rt.storage.artifacts.resolve(state.final)


def summaries(h: Harness, run_id: str) -> list[str]:
    return [e.summary for e in h.rt.storage.events.tail(run_id, limit=10_000)]


async def test_end_card_placeholder_brand_is_flagged_everywhere(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path))
    state = await run(h)
    assert state.stage is Stage.DONE and state.endcard is not None
    assert state.endcard.brand_name == "Nocturne Nine" and state.endcard.is_placeholder
    info = probe(final_path(h, state))
    assert float(info["format"]["duration"]) == pytest.approx(17.0, abs=0.1)  # 15 s + 2 s card
    text = summaries(h, state.id)
    assert any("End card: Nocturne Nine" in s and "PLACEHOLDER" in s for s in text)
    assert any(s.startswith("Duration check: 17.0") for s in text)
    pack = h.rt.storage.artifacts.run_dir(state.id) / "production_pack"
    assert "**PLACEHOLDER**" in (pack / "storyboard.md").read_text()
    data = json.loads((pack / "storyboard.json").read_text())
    assert data["endcard"]["brand_placeholder"] is True and "PLACEHOLDER" in data["endcard"]["note"]


async def test_brand_from_the_brief_is_used_without_an_llm_call(tmp_path: Path) -> None:
    understand = json.loads((FIXTURES_DIR / "perfume_en" / "understand.json").read_text())
    branded = copy.deepcopy(understand)
    branded[-1]["output"] |= {
        "brand_name": "Saqr",
        "tagline": "Own the night.",
        "tagline_language": "en",
    }
    h = make_harness(fast_settings(tmp_path), scripts={"understand": branded})
    state = await run(h)
    assert state.endcard is not None and not state.endcard.is_placeholder
    assert (state.endcard.brand_name, state.endcard.tagline) == ("Saqr", "Own the night.")
    assert h.llm is not None and h.llm.calls_for("endcard_copy") == 0


async def test_a_real_brand_proposal_is_repaired(tmp_path: Path) -> None:
    real = {"output": {"brand_name": "Dior Noir", "tagline": "Some nights remember you."}}
    fake = {"output": {"brand_name": "Nocturne Nine", "tagline": "Some nights remember you."}}
    h = make_harness(fast_settings(tmp_path), scripts={"endcard_copy": [real, fake]})
    state = await run(h)
    assert state.endcard is not None and state.endcard.brand_name == "Nocturne Nine"
    assert h.llm is not None and h.llm.calls_for("endcard_copy") == 2
    assert real_brand_in("عطر شانيل الجديد") == "شانيل" and real_brand_in("Nocturne Nine") is None


async def test_slate_mode_keeps_the_ads_timing(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path, partial_mode="slate"), fail_plan="2:clip:permanent")
    state = await run(h)
    assert state.stage is Stage.DONE_PARTIAL
    assert float(probe(final_path(h, state))["format"]["duration"]) == pytest.approx(17.0, abs=0.1)
    assert any("slate for failed scene(s) [2]" in s for s in summaries(h, state.id))


async def test_skip_mode_is_the_default(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path), fail_plan="2:clip:permanent")
    state = await run(h)
    assert state.config.partial_mode == "skip"
    assert any("skipped failed scene(s) [2]" in s for s in summaries(h, state.id))


async def test_user_music_is_mixed_and_trimmed(tmp_path: Path) -> None:
    music = tmp_path / "tone.m4a"
    subprocess.run(  # noqa: ASYNC221 - test setup, not agent code
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=330:duration=5",
         "-c:a", "aac", str(music)],
        check=True,
    )  # fmt: skip
    h = make_harness(fast_settings(tmp_path, music_path=music))
    state = await run(h)
    info = probe(final_path(h, state))
    assert {s["codec_type"] for s in info["streams"]} == {"video", "audio"}
    assert float(info["format"]["duration"]) == pytest.approx(17.0, abs=0.15)


async def test_edit_reassembly_keeps_the_same_end_card(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path), scenario="perfume_ar")
    state = await run(h)
    assert state.endcard is not None and state.endcard.brand_name == "عَبَق"
    outcome = await EditService(h.rt).edit(state.id, "خل المشهد الثاني أبطأ")
    assert outcome.status == "applied"
    again = await h.orch.run_until_blocked(state.id)
    assert again.endcard == state.endcard
    assert h.llm is not None and h.llm.calls_for("endcard_copy") == 1


def test_bundled_font_covers_every_arabic_letter_form() -> None:
    letters = "ءآأؤإئابةتثجحخدذرزسشصضطظعغفقكلمنهوىي"
    # every letter alone, initial, medial and final (tatweel forces the joining forms)
    sample = " ".join(f"{c} {c}ـ ـ{c}ـ ـ{c}" for c in letters) + " لا لأ لإ لآ"
    assert missing_glyphs(sample, font(40)) == []
    assert missing_glyphs(sample, font(40, "Bold")) == []


def test_shaping_joins_letters_and_reverses_for_rtl() -> None:
    shaped = shape("عبق")
    assert shaped != "عبق" and all("ﹰ" <= ch <= "﻿" for ch in shaped)
    assert shape("Nocturne Nine") == "Nocturne Nine"
    assert EventType.DECISION.value == "decision"
