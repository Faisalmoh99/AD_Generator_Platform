"""PLAN_V2 item 4: the cultural wardrobe lexicon, its bible lint and its prompt injection."""

from __future__ import annotations

from pathlib import Path

from adsmith.config import Settings
from adsmith.domain.continuity import CharacterSpec
from adsmith.domain.enums import Dialect, Language
from tests.factories import make_brief
from tests.media_helpers import fast_settings, make_harness

STEP_A_WARDROBE = "white thobe with a mandarin collar and a visible button placket"


def character(*wardrobe: str) -> CharacterSpec:
    return CharacterSpec(
        description="young Saudi man",
        age_range="22-28",
        appearance="trimmed beard",
        hair="short black",
        grooming="neat",
        build="slim",
        wardrobe=list(wardrobe),
    )


def test_lint_catches_the_step_a_kurta_phrasing(settings: Settings) -> None:
    brief = make_brief(raw_request="عطر رجالي", language=Language.AR, dialect=Dialect.SAUDI_NAJDI)
    problems = settings.culture.wardrobe_problems(brief, character(STEP_A_WARDROBE))
    text = "; ".join(problems)
    assert "'mandarin collar'" in text and "South Asian" in text
    assert "'button placket'" in text and "concealed placket" in text


def test_negated_and_correct_wardrobe_passes(settings: Settings) -> None:
    brief = make_brief(cultural_notes=["Saudi audience"])
    ok = character(
        "white Saudi thobe, stiff stand collar, concealed placket with no visible buttons"
    )
    assert settings.culture.wardrobe_problems(brief, ok) == []
    assert settings.culture.wardrobe_problems(brief, None) == []


def test_lexicon_applies_only_to_gulf_briefs(settings: Settings) -> None:
    lex = settings.culture
    ar = make_brief(raw_request="عطر", language=Language.AR, dialect=Dialect.SAUDI_NAJDI)
    saudi_en = make_brief(raw_request="A perfume ad for young Saudi men")
    london = make_brief(raw_request="A perfume ad for young men in London")
    assert lex.applicable(ar) and lex.applicable(saudi_en) and not lex.applicable(london)
    assert lex.prompt_block(london) == "(none)"
    assert "concealed front placket" in lex.prompt_block(ar)
    assert lex.wardrobe_problems(london, character(STEP_A_WARDROBE)) == []


async def test_lexicon_reaches_continuity_prompt_writer_and_gate(tmp_path: Path) -> None:
    h = make_harness(fast_settings(tmp_path, qa_mode="llm"), scenario="perfume_ar", scripts=None)
    assert h.llm is not None
    run = h.orch.create_run("سوّ لي إعلان ١٥ ثانية لعطر رجالي فاخر", interactive=False)
    await h.orch.run_until_blocked(run.id)

    def text_of(prefix: str, needle: str) -> list[str]:
        out = []
        for r in h.llm.requests:  # type: ignore[union-attr]
            if r.template.startswith(prefix):
                body = "\n".join(
                    getattr(p, "text", "") for m in r.messages for p in getattr(m, "parts", ())
                )
                out.append(body)
        return [b for b in out if needle in b]

    marker = "concealed front placket"
    assert text_of("continuity@", marker)
    assert text_of("prompt_writer@", marker)  # only shots with a person get it
    assert text_of("qa_keyframe@", marker)
