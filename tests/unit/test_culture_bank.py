"""ADR-0026 phase 4: the Saudi culture bank (data) and the insight stage's culture anchor."""

from __future__ import annotations

import copy
import re
from typing import Any

import pytest

from adsmith.agent.nodes.common import insights_block
from adsmith.config import PROJECT_ROOT, Settings
from adsmith.domain.brief import Brief
from adsmith.domain.concept import InsightSet
from adsmith.domain.culture_bank import CultureBank
from adsmith.domain.enums import Stage
from tests.factories import make_full_run
from tests.unit.test_orchestrator import full_scripts, make_orch

_PRESENTATION_FORMS = re.compile(r"[ﭐ-﷿ﹰ-﻿]")


@pytest.fixture(scope="module")
def bank() -> CultureBank:
    return Settings().culture_bank


def brief(category: str, request: str, product: str = "a product") -> Brief:
    b = make_full_run(Stage.INSIGHT).brief
    assert b is not None
    return b.model_copy(
        update={"product_category": category, "raw_request": request, "product": product}
    )


def test_the_bank_has_about_thirty_entries_in_logical_order(bank: CultureBank) -> None:
    assert len(bank.entries) >= 30
    raw = (PROJECT_ROOT / "config" / "culture_bank.toml").read_text(encoding="utf-8")
    assert not _PRESENTATION_FORMS.search(raw)
    assert all(re.search(r"[؀-ۿ]", e.text_ar) for e in bank.entries)


@pytest.mark.parametrize(
    ("category", "request_text", "expected", "absent"),
    [
        ("Perfume / fragrance", "اعلان لعطر نسائي", "bakhoor_farewell", "shake_the_cup"),
        ("Specialty coffee", "اعلان لقهوة سعودية مختصة", "shake_the_cup", "bakhoor_farewell"),
        ("Premium dates", "اعلان لتمر فاخر", "ruttab_season", "bakhoor_farewell"),
    ],
)
def test_a_brief_gets_the_entries_of_its_category_first(
    bank: CultureBank, category: str, request_text: str, expected: str, absent: str
) -> None:
    entries = bank.for_brief(brief(category, request_text))
    ids = [e.id for e in entries]
    assert expected in ids and absent not in ids and len(ids) <= 12
    general = [i for i, e in enumerate(entries) if e.categories == ["any"]]
    own = [i for i, e in enumerate(entries) if e.categories != ["any"]]
    assert not general or not own or max(own) < min(general)  # its own category first


def test_an_arabic_request_alone_finds_the_category(bank: CultureBank) -> None:
    assert bank.categories_for(brief("Consumer product", "اعلان لتمر فاخر")) == ["dates"]
    assert "coffee" not in bank.categories_for(brief("Perfume", "an ad", product="update kit"))


def test_the_prompt_block_carries_meaning_use_and_pitfalls(bank: CultureBank) -> None:
    entry = bank.get("national_day")
    assert entry is not None
    text = bank.prompt_block([entry])
    assert "«اليوم الوطني»" in text and "An ad can use it as:" in text
    assert "Never: the flag carries the shahada" in text
    assert "anchor every insight to 'none'" in bank.prompt_block([])


def test_an_old_insight_without_an_anchor_still_loads() -> None:
    old = {
        "category_cliches": ["a", "b", "c"],
        "insights": [
            {"statement": "s", "tension": "t", "evidence": "e", "gulf_meaning": "g"}
            for _ in range(3)
        ],
    }
    found = InsightSet.model_validate(old)
    assert [x.culture_anchor for x in found.insights] == ["none"] * 3


def test_ideate_sees_the_anchor_meaning(bank: CultureBank) -> None:
    found = InsightSet.model_validate(
        {
            "category_cliches": ["a", "b", "c"],
            "insights": [
                {
                    "statement": "s",
                    "tension": "t",
                    "evidence": "e",
                    "gulf_meaning": "g",
                    "culture_anchor": a,
                }
                for a in ("bakhoor_farewell", "none", "none")
            ],
        }
    )
    text = insights_block(found, bank)
    assert "culture anchor: «تبخير الضيف قبل انصرافه»" in text
    assert text.count("culture anchor") == 1


async def test_an_unknown_anchor_goes_back_to_the_model(settings: Settings) -> None:
    scripts: dict[str, Any] = full_scripts()
    good = scripts["insight"][0]
    bad = copy.deepcopy(good)
    bad["output"]["insights"][0]["culture_anchor"] = "camel_race"
    scripts["insight"] = [bad, good]
    orch, provider = make_orch(settings, scripts)
    run = orch.create_run("an ad for a men's perfume", interactive=False)
    state = await orch.run_until_blocked(run.id, stop_before=Stage.IDEATE)
    assert state.insights is not None and provider.calls_for("insight") == 2
    repair = provider.requests[-1].messages[-1]
    parts: tuple[Any, ...] = getattr(repair, "parts", ())
    assert "culture_anchor ['camel_race'] is not in the culture bank" in parts[0].text
    req = next(r for r in provider.requests if r.template == "insight@v2")
    body = "\n".join(p.text for p in getattr(req.messages[0], "parts", ()))
    assert "<culture_bank>" in body and "bakhoor_farewell" in body


# ----------------------------------------------------------------------------- Gulf objects


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("dallah", "dallah"), ("brass coffee pot", "dallah"), ("دلة", "dallah"),
        ("small coffee cup", "finjan"), ("incense burner", "mabkhara"),
        ("wooden chest", "mandoos"), ("hospitality tray", None),
    ],
)  # fmt: skip
def test_a_prop_name_finds_its_gulf_object(
    bank: CultureBank, name: str, expected: str | None
) -> None:
    obj = bank.object_for(name)
    assert (obj.id if obj else None) == expected


def test_the_dallah_is_described_as_the_gulf_version(bank: CultureBank) -> None:
    obj = bank.object_for("dallah")
    assert obj is not None
    assert "crescent beak spout" in obj.description and "pinched at the waist" in obj.description
    assert any("gooseneck" in n for n in obj.not_like)
    entry = bank.get("dallah_on_the_fire")
    assert entry is not None and entry.objects == ["dallah"]


def test_code_puts_the_gulf_description_into_the_prompt(bank: CultureBank) -> None:
    from adsmith.domain.ad import AdPromptDraft  # noqa: PLC0415
    from adsmith.domain.culture_bank import with_gulf_objects  # noqa: PLC0415
    from tests.unit.test_creative_rules import good  # noqa: PLC0415

    pot = {
        "name": "dallah", "description": "a tall brass coffee pot with a long curved spout",
        "role": "prop", "shots": [2], "transforms_into_product": False, "handled": True,
        "max_size": "half", "lookalike": False,
    }  # fmt: skip
    d = AdPromptDraft.model_validate(good().model_dump() | {"references": [pot]})
    out = with_gulf_objects(d, bank)
    assert "crescent beak spout" in out.references[0].description


def test_hospitality_pitfalls_reach_every_prompt(bank: CultureBank) -> None:
    assert any("ringing bell is answered" in p for p in bank.general_pitfalls)
    for eid in ("bakhoor_before_guests", "coffee_right_hand", "dallah_on_the_fire", "the_majlis"):
        entry = bank.get(eid)
        assert entry is not None
        assert any("never ignores or delays a guest" in p for p in entry.pitfalls), eid
    text = bank.prompt_block([])
    assert text.endswith("Never, in any ad: " + "; ".join(bank.general_pitfalls) + ".")
