"""Fixes from the perfume video and the 2026-11-07 runs: an unheld product names its support,
a reunion ends on both people, four repair rounds for the writer and the director with a 10 %
word tolerance, the production pack follows the final prompt, and the Kie key's source."""

from __future__ import annotations

from pathlib import Path

import pytest

from adsmith.config import LLMNode, Settings, secret_source
from adsmith.domain.creative_rules import (
    CreativeRules,
    creative_rule_problems,
    product_share,
    unsupported_shots,
    word_budget_problems,
    word_budget_warnings,
)
from tests.unit.test_creative_rules import draft, shot

# The perfume video's shot 2, as sent (run_01M6VG1SG2R0E76X3HXMEVPPNP).
FLOATING = (
    "From the open bottle, the camera follows a finger-wide golden mist ribbon between palm "
    "trunks to his seated mother; she looks up and smiles."
)


@pytest.fixture(scope="module")
def rules() -> CreativeRules:
    return Settings().creative_rules


def perfume(action2: str) -> object:
    return draft(
        [
            shot(1, 0, 1.5, handled=True, action="The bottle rests in his palm; he lifts the cap."),
            shot(2, 1.5, 4, action=action2, product_in_frame="small"),
            shot(3, 4, 6, handled=True, action="His fingers grip the bottle by its body."),
        ],
        key_shot=2,
    )


def test_an_unheld_product_with_nothing_under_it_is_sent_back(rules: CreativeRules) -> None:
    d = perfume(FLOATING)
    assert unsupported_shots(d, rules.support) == [2]  # type: ignore[arg-type]
    text = " ".join(creative_rule_problems(d, rules))  # type: ignore[arg-type]
    assert "shot 2 shows the product with no hand on it and no surface under it" in text
    seen, _ = product_share(d, rules.support)  # type: ignore[arg-type]
    assert seen == [1, 3]  # the floating shot does not count toward the product shots


def test_a_palm_tree_is_not_a_palm(rules: CreativeRules) -> None:
    sup = rules.support
    assert sup is not None
    assert not sup.supported("between palm trunks to his mother")
    assert sup.supported("resting in her open palm") and sup.supported("on the wooden bench")
    assert sup.supported("gripped between thumb and fingers")
    assert not sup.supported("the bottle in the sunset light")


def test_a_named_surface_or_a_hand_is_enough(rules: CreativeRules) -> None:
    on_bench = perfume("The open bottle stands on the bench beside her; she looks up and smiles.")
    assert unsupported_shots(on_bench, rules.support) == []  # type: ignore[arg-type]
    seen, _ = product_share(on_bench, rules.support)  # type: ignore[arg-type]
    assert seen == [1, 2, 3]
    assert "try_8" in rules.lessons and "floated" in rules.lessons["try_8"]


def test_a_reunion_ends_on_both_people(rules: CreativeRules) -> None:
    rule = next(r for r in rules.judge if r.id == "reunion_payoff")
    assert "BOTH people" in rule.text and "never hands alone" in rule.text
    from adsmith.agent.prompts import PromptLibrary  # noqa: PLC0415

    critic = PromptLibrary(Settings().prompts_dir).get("qa_ad_critic")
    assert critic.version == 5 and "A reunion payoff" in critic.system


def test_the_writer_and_the_director_get_four_repair_rounds() -> None:
    s = Settings()
    assert s.max_repairs(LLMNode.PROMPT_WRITER) == 4 and s.max_repairs(LLMNode.DIRECTOR) == 4
    assert s.max_repairs(LLMNode.IDEATE) == 2 and s.max_repairs(LLMNode.COPYWRITER) == 2


def test_ten_percent_over_is_a_warning_not_a_failure(rules: CreativeRules) -> None:
    p = rules.params
    words = " ".join(["word"] * (p.max_words_shot + 3 - 5))  # camera and sound add 5 words
    soft = draft([shot(1, 0, 2, action=words), shot(2, 2, 4), shot(3, 4, 6)])
    assert word_budget_problems(soft, p) == []
    assert any(w.startswith("shot 1 38 words (budget 35") for w in word_budget_warnings(soft, p))
    long_ = " ".join(["word"] * (p.max_words_shot + 6 - 5))  # 41 > 38.5
    hard = draft([shot(1, 0, 2, action=long_), shot(2, 2, 4), shot(3, 4, 6)])
    assert any("shot 1 takes 41 words" in m for m in word_budget_problems(hard, p))


def test_the_key_source_never_shows_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("KIE_API_KEY=file-key-123\n")
    monkeypatch.delenv("KIE_API_KEY", raising=False)
    assert secret_source("KIE_API_KEY", env) == ".env"
    monkeypatch.setenv("KIE_API_KEY", "file-key-123")
    assert secret_source("KIE_API_KEY", env).startswith("shell environment (the same key")
    monkeypatch.setenv("KIE_API_KEY", "shell-key-456")
    msg = secret_source("KIE_API_KEY", env)
    assert "DIFFERENT" in msg and "env -u KIE_API_KEY" in msg
    assert "456" not in msg and "123" not in msg
    assert secret_source("KIE_API_KEY", tmp_path / "missing") == "shell environment"
    monkeypatch.delenv("KIE_API_KEY")
    assert secret_source("KIE_API_KEY", tmp_path / "missing") == "not set"
