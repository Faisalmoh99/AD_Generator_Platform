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
    text = CultureBank.prompt_block([entry])
    assert "«اليوم الوطني»" in text and "An ad can use it as:" in text
    assert "Never: the flag carries the shahada" in text
    assert "anchor every insight to 'none'" in CultureBank.prompt_block([])


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
