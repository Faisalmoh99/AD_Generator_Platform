"""ADR-0026 phase 1 end to end (zero keys): the v2 writer sees the product photo, and the
prompt that reaches the spend gate has the human-directed structure with a References block."""

from __future__ import annotations

from pathlib import Path

from adsmith.agent.orchestrator import Orchestrator
from adsmith.domain.enums import Stage
from adsmith.providers.llm.base import ImagePart
from adsmith.providers.llm.scripted import ScriptedLLMProvider
from tests.integration.test_multishot_pipeline import REQUEST, photo, runtime, settings


async def test_the_writer_sees_the_photo_and_the_prompt_has_references(tmp_path: Path) -> None:
    rt = runtime(settings(tmp_path))
    llm = rt._provider_override
    assert isinstance(llm, ScriptedLLMProvider)
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    state = await orch.run_until_blocked(run.id, stop_before=Stage.ESTIMATE)
    assert state.stage is Stage.ESTIMATE and state.generation is not None
    req = next(r for r in llm.requests if r.template == "ad_prompt_writer@v4")
    parts = getattr(req.messages[0], "parts", ())
    assert any(isinstance(p, ImagePart) for p in parts)
    p = state.generation.prompt
    assert p.template_version == "ad_prompt_writer@v4" and p.key_shot == 3
    lines = p.text.splitlines()
    assert lines[1].startswith("References: Image 1 is the product")
    # The PROPS stage generated both props, so the References line numbers them.
    assert "Image 2 is the brass tray: a round, flat, hammered brass tray" in lines[1]
    assert "Image 3 is the coffee cup" in lines[1]
    assert lines[2].startswith("Look: An empty traditional majlis")
    assert "Framing: the product fills about half the frame." in lines[5]
    assert [s.handled for s in p.shots] == [False, False, True, False]


async def test_the_pack_shows_every_concept_with_its_six_scores(tmp_path: Path) -> None:
    rt = runtime(settings(tmp_path))
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    state = await orch.run_until_blocked(run.id, stop_before=Stage.ESTIMATE)
    pack = rt.storage.artifacts.run_dir(state.id) / "production_pack" / "storyboard.md"
    text = pack.read_text()
    assert "## Concepts and rubric" in text
    assert "| # | concept | insight | hook | story_turn |" in text
    assert text.count("(picked)") == 1
    assert "## Director review: kept" in text and "References sent with the prompt:" in text
