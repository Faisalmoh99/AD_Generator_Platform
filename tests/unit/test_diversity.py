"""P4 (ADR-0028 part 3): concept diversity. Axes assigned by code, six candidates on a whole-ad
run, code keeps the three least alike and away from recent winners' motifs, the ledger records
each winner, the novelty penalty, and a culture bank balanced by theme. Zero keys."""

from __future__ import annotations

from pathlib import Path

import pytest

from adsmith.agent.orchestrator import Orchestrator
from adsmith.config import Settings
from adsmith.domain.concept import (
    MotifEntry,
    assign_axes,
    motif_overlap,
    pick_distinct,
)
from adsmith.domain.enums import Stage
from adsmith.domain.rubric import RUBRIC_KEYS
from adsmith.storage import Storage
from tests.integration.test_multishot_pipeline import REQUEST, photo, runtime, settings

HOST = MotifEntry(run_id="run_old", title="Before the knock", axis="hospitality",
                  motifs=["host", "last guest", "doorway"])  # fmt: skip


def test_axes_start_from_the_insights_and_hospitality_needs_an_anchor() -> None:
    assert assign_axes(["hospitality", "craft", "hospitality"], 6) == [
        "hospitality", "craft", "self", "family", "travel", "season",
    ]  # fmt: skip
    assert "hospitality" not in assign_axes([], 6)
    assert assign_axes(["self", "self", "family"], 3) == ["self", "family", "craft"]


def test_motifs_match_on_whole_content_words() -> None:
    assert motif_overlap(["front door", "host"], ["door", "guest"]) == 1
    assert motif_overlap(["hostess"], ["host"]) == 0
    assert motif_overlap(["host", "last guest", "doorway"], HOST.motifs) == 3


def test_three_written_means_three_kept() -> None:
    assert pick_distinct([], [], [HOST]) == []


async def test_six_are_written_and_the_three_least_alike_are_kept(tmp_path: Path) -> None:
    rt = runtime(settings(tmp_path))
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    state = await orch.run_until_blocked(run.id, stop_before=Stage.STORYBOARD)
    assert state.concepts is not None
    titles = [c.title for c in state.concepts.concepts]
    assert titles == ["The Last Guest", "Amber Hour", "First Rain"]
    assert [c.axis for c in state.concepts.concepts] == ["hospitality", "self", "season"]
    dropped = state.concepts.candidates_dropped  # stored in full, not only their axes
    assert [c.title for c in dropped] == ["Wedding Night Walk", "The Blender's Nose", "Mile Zero"]
    assert dropped[1].story is not None and dropped[1].motifs == [
        "perfumer",
        "workshop",
        "blotters",
    ]
    evs = [e.summary for e in rt.storage.events.tail(run.id, limit=2000)]
    assert any(e.startswith("Diversity: 6 concepts written on axes hospitality, self") for e in evs)


async def test_a_recent_winners_motifs_are_avoided_and_the_winner_is_recorded(
    tmp_path: Path,
) -> None:
    rt = runtime(settings(tmp_path, motif_ledger_scripted=True))
    rt.storage.motifs.record(HOST, source="seed")
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    state = await orch.run_until_blocked(run.id, stop_before=Stage.CONTINUITY)
    assert state.concepts is not None
    titles = [c.title for c in state.concepts.concepts]
    assert "The Last Guest" not in titles  # host + last guest + doorway = a recent winner
    llm = rt._provider_override
    asked = next(r for r in llm.requests if r.template == "ideate@v9")  # type: ignore[union-attr]
    assert "Before the knock [hospitality]: host, last guest, doorway" in str(asked.messages)
    ledger = rt.storage.motifs.recent(10)
    winner = state.concepts.selected
    assert winner is not None and ledger[0].run_id == run.id and ledger[0].title == winner.title


def test_the_novelty_penalty_moves_the_winner() -> None:
    from adsmith.domain.concept import ConceptJudgementDraft  # noqa: PLC0415

    rubric = Settings().rubric
    assert rubric.novelty_penalty == 1.0

    def score(n: int, v: int) -> dict[str, object]:
        return {"concept_number": n, **dict.fromkeys(RUBRIC_KEYS, v),
                "uses_category_cliche": False, "rationale": "x"}  # fmt: skip

    j = ConceptJudgementDraft.model_validate({"scores": [score(1, 8), score(2, 7), score(3, 6)]})
    assert j.winner(rubric).concept_number == 1
    assert j.winner(rubric, {1: 1.5}).concept_number == 2


def test_the_ledger_round_trips(tmp_path: Path) -> None:
    s = Storage.open(tmp_path)
    s.motifs.record(HOST)
    s.motifs.record(HOST.model_copy(update={"title": "again"}))  # one row per run
    got = s.motifs.recent(10)
    assert len(got) == 1 and got[0].title == "again" and got[0].motifs == HOST.motifs
    assert s.motifs.recent(10, exclude="run_old") == []


def test_every_culture_entry_has_a_theme_and_the_sample_is_balanced() -> None:
    bank = Settings().culture_bank

    class Coffee:
        product_category, product, raw_request = "coffee", "saudi coffee", "اعلان لقهوة سعودية"

    picked = bank.for_brief(Coffee())  # type: ignore[arg-type]
    themes = [e.theme for e in picked]
    assert len(picked) == 12 and max(themes.count(t) for t in set(themes)) <= 3
    assert {"hospitality", "craft", "self", "travel", "season"} <= set(themes)


@pytest.mark.parametrize("axis", ["travel", "craft"])
def test_the_bank_has_non_hospitality_coffee_entries(axis: str) -> None:
    bank = Settings().culture_bank
    assert any(e.theme == axis and "coffee" in e.categories for e in bank.entries)


async def test_zero_key_runs_never_fill_the_ledger_by_default(tmp_path: Path) -> None:
    rt = runtime(settings(tmp_path))
    orch = Orchestrator(rt)
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    await orch.run_until_blocked(run.id, stop_before=Stage.CONTINUITY)
    assert rt.storage.motifs.recent(10) == []
