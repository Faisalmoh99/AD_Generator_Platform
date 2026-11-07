"""Saudi qahwa is served from a dallah (user rule, 2026-11-07): a code check on every whole-ad
prompt, a judged rule for the director, the culture bank's serving facts, and the human command
that rewrites a prompt in place (concept, copy and end card kept)."""

from __future__ import annotations

from pathlib import Path

import pytest

from adsmith.agent import commands
from adsmith.agent.commands import CommandRejectedError
from adsmith.agent.orchestrator import Orchestrator
from adsmith.config import Settings
from adsmith.domain.creative_rules import CreativeRules, serving_problems
from adsmith.domain.enums import Stage
from adsmith.providers.llm.scripted import ScriptedLLMProvider
from tests.integration.test_multishot_pipeline import REQUEST, photo, runtime, settings
from tests.unit.test_creative_rules import draft, shot


@pytest.fixture(scope="module")
def rules() -> CreativeRules:
    return Settings().creative_rules


def prompt(action: str, refs: list[dict[str, object]] | None = None) -> object:
    return draft(
        [
            shot(1, 0, 2, handled=True),
            shot(2, 2, 4, action=action, event="the pour"),
            shot(3, 4, 6.5),
            shot(4, 6.5, 9, product_in_frame="fills"),
        ],
        references=refs or [],
    )


def ref(name: str) -> dict[str, object]:
    return {"name": name, "description": "a clear glass carafe with a black handle", "role": "prop",
            "shots": [2], "transforms_into_product": False, "handled": True, "max_size": "half",
            "lookalike": False}  # fmt: skip


def test_qahwa_from_a_glass_brewer_is_sent_back(rules: CreativeRules) -> None:
    from_the_run = (  # the coffee run run_01M6TBP6H8VTXWGXFJR6XPVQ7M, shot 2
        "The father, lifting the glass brewer by its handle, tilts it slowly; a thin coffee "
        "ribbon fills the son's finjan on the table."
    )
    msgs = serving_problems(prompt(from_the_run), rules.serving)  # type: ignore[arg-type]
    assert any('serves into a finjan from a "glass brewer"' in m and "dallah" in m for m in msgs)


def test_pouring_into_a_finjan_must_name_the_dallah(rules: CreativeRules) -> None:
    bare = "The father tilts it slowly and a thin ribbon of coffee fills the son's finjan."
    msgs = serving_problems(prompt(bare), rules.serving)  # type: ignore[arg-type]
    assert any("without naming the dallah" in m for m in msgs)
    ok = (
        "The father lifts the dallah in his left hand and pours a thin ribbon of golden coffee "
        "into the son's finjan."
    )
    assert serving_problems(prompt(ok), rules.serving) == []  # type: ignore[arg-type]


def test_a_brewer_prop_next_to_a_finjan_is_refused(rules: CreativeRules) -> None:
    ok = "The father lifts the dallah and pours a thin ribbon into the son's finjan."
    msgs = serving_problems(prompt(ok, [ref("glass brewer")]), rules.serving)  # type: ignore[arg-type]
    assert any('reference "glass brewer"' in m for m in msgs)


def test_pour_over_without_a_finjan_is_fine(rules: CreativeRules) -> None:
    specialty = "She tilts the glass brewer and a thin ribbon of dark coffee fills a white mug."
    assert serving_problems(prompt(specialty, [ref("glass brewer")]), rules.serving) == []  # type: ignore[arg-type]


def test_the_director_judges_serving_and_the_bank_knows_the_hands(rules: CreativeRules) -> None:
    assert "serving_correct" in rules.judge_ids
    text = next(r.text for r in rules.judge if r.id == "serving_correct")
    assert "dallah in the left hand" in text and "an elder pouring for a younger" in text
    assert "Serving Saudi qahwa (checked by code)" in rules.prompt_block()
    bank = Settings().culture_bank
    entry = bank.get("coffee_right_hand")
    assert entry is not None and "dallah in the left hand" in entry.meaning_en
    dallah = bank.object_for("dallah")
    assert dallah is not None and any("glass brewer" in x for x in dallah.not_like)


async def test_a_prompt_is_rewritten_in_place_and_the_copy_is_kept(tmp_path: Path) -> None:
    rt = runtime(settings(tmp_path))
    llm = rt._provider_override
    assert isinstance(llm, ScriptedLLMProvider)
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    before = await orch.run_until_blocked(run.id, stop_before=Stage.ESTIMATE)
    assert before.copy_selection is not None and len(before.reference_images) == 2
    commands.rewrite_prompt(rt.storage, run.id, reason="serving rule")
    after = await orch.run_until_blocked(run.id, stop_before=Stage.ESTIMATE)
    assert after.stage is Stage.ESTIMATE
    assert llm.calls_for("ad_prompt_writer") == 2 and llm.calls_for("director") == 2
    assert llm.calls_for("copywriter") == 1  # the copy was kept, not written again
    assert after.copy_selection == before.copy_selection and after.endcard == before.endcard
    assert after.concepts == before.concepts
    assert [r.name for r in after.reference_images] == ["brass tray", "coffee cup"]
    evs = [e.summary for e in rt.storage.events.tail(run.id, limit=3000)]
    assert any(e.startswith("HUMAN: rewrite the whole-ad prompt (serving rule)") for e in evs)


async def test_a_paid_generation_is_never_rewritten_in_place(tmp_path: Path) -> None:
    rt = runtime(settings(tmp_path))
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    done = await orch.run_until_blocked(run.id)
    assert done.stage is Stage.DONE
    with pytest.raises(CommandRejectedError, match="regenerate-ad"):
        commands.rewrite_prompt(rt.storage, run.id, reason="too late")
