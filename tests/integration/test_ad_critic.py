"""Phase D: the whole-ad critic (ADR-0025). The hard gate decides `passed` by code; the creative
rubric score is computed by code and only WARNS; nothing is regenerated automatically."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from adsmith.agent.orchestrator import Orchestrator
from adsmith.config import Settings
from adsmith.domain.ad import AdGeneration
from adsmith.domain.enums import Stage
from adsmith.domain.events import EventType
from adsmith.domain.qa import AdVerdict, AdVerdictDraft
from adsmith.domain.rubric import RUBRIC_KEYS
from adsmith.providers.llm.scripted import ScriptedLLMProvider
from tests.integration.test_multishot_pipeline import (
    REQUEST,
    mock_provider,
    photo,
    runtime,
    settings,
)

CHECKS = {
    "product_visible": True,
    "matches_shot_spec": True,
    "lighting_matches": True,
    "continuity_ok": True,
    "artifacts_free": True,
    "brand_safe": True,
    "text_free": True,
    "alcohol_free": True,
    "key_action_happens": True,
}


def verdict_output(**over: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "score": 0.82,
        "checks": dict(CHECKS),
        "issues": [],
        "revised_prompt_hint": "",
        "frames_inspected": 13,
        "shots": [
            {"number": n, "delivered": True, "note": f"shot {n} reads clearly"}
            for n in (1, 2, 3, 4)
        ],
        "rubric": {
            "insight": 8,
            "hook": 7,
            "story_turn": 8,
            "originality": 7,
            "cultural_meaning": 9,
            "product_role": 8,
        },
        "creative_note": "The hand-over lands as a real gesture; the opening is a little slow.",
    }
    return out | over


def llm_run(tmp_path: Path, steps: list[dict[str, Any]]) -> tuple[Orchestrator, Any]:
    rt = runtime(settings(tmp_path, qa_mode="llm"))
    assert isinstance(rt._provider_override, ScriptedLLMProvider)
    rt._provider_override.override("qa_ad_critic", steps)
    return Orchestrator(rt), rt


def events(rt: Any, run_id: str) -> list[Any]:
    return list(rt.storage.events.tail(run_id, limit=3000))


# ----------------------------------------------------------------------------- the verdict rules


def ad_verdict(**over: Any) -> AdVerdict:
    from adsmith.domain.qa import QAVerdict, QAVerdictDraft  # noqa: PLC0415

    d = AdVerdictDraft.model_validate(verdict_output(**over))
    core = QAVerdictDraft.model_validate(d.model_dump(include=set(QAVerdictDraft.model_fields)))
    base = QAVerdict.from_draft(core, threshold=0.7)
    rubric = Settings(_env_file=None).rubric
    return AdVerdict(
        **base.model_dump(),
        shots=d.shots,
        rubric=d.rubric,
        creative_note=d.creative_note,
        creative_score=rubric.weighted(d.rubric.model_dump()),
        creative_threshold=6.5,
    )


def test_a_hard_failure_beats_a_perfect_rubric() -> None:
    v = ad_verdict(
        checks=CHECKS | {"text_free": False},
        issues=[
            {
                "category": "artifact",
                "severity": "major",
                "observation": "letters on the cap",
                "fix": "no text",
                "frame_index": 5,
            }
        ],
        rubric=dict.fromkeys(RUBRIC_KEYS, 10),
    )
    assert v.creative_score == pytest.approx(10.0) and v.creative_pass
    assert not v.passed and "text, logo or garbled letters in frame" in v.fail_reasons


def test_a_low_creative_score_is_a_warning_never_a_failure() -> None:
    v = ad_verdict(
        rubric={
            "insight": 3,
            "hook": 4,
            "story_turn": 3,
            "originality": 4,
            "cultural_meaning": 3,
            "product_role": 6,
        },
        shots=[
            {"number": 1, "delivered": False, "note": "no hook"},
            {"number": 2, "delivered": True, "note": "ok"},
        ],
    )
    assert v.passed and not v.creative_pass
    assert v.creative_score == pytest.approx(0.2 * 3 + 0.2 * 4 + 0.15 * (3 + 4 + 3 + 6))
    assert v.warnings == [
        f"creative score {v.creative_score:.2f} < 6.5",
        "planned beat not delivered in shot(s) [1]",
    ]


def test_the_verdict_survives_a_snapshot_round_trip(tmp_path: Path) -> None:
    from tests.factories import make_video  # noqa: PLC0415
    from tests.unit.test_ad_generation import attempt, generation  # noqa: PLC0415

    g = generation()
    g.set_estimate(3.6)
    g.approve("user")
    g.start(attempt())
    g.complete(make_video("ad"))
    g.review(ad_verdict())
    again = AdGeneration.model_validate_json(g.model_dump_json())
    assert isinstance(again.qa, AdVerdict) and again.qa.creative_score == g.qa.creative_score  # type: ignore[union-attr]
    assert again.qa.rubric.cultural_meaning == 9


# ----------------------------------------------------------------------------- end to end


async def test_the_critic_sees_the_photo_and_three_frames_per_shot(tmp_path: Path) -> None:
    orch, rt = llm_run(tmp_path, [{"output": verdict_output()}])
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    state = await orch.run_until_blocked(run.id)
    assert state.stage is Stage.DONE
    g = state.generation
    assert g is not None and isinstance(g.qa, AdVerdict) and g.status == "approved"
    assert g.qa.frames_inspected == 13  # the photo + 4 shots × 3 frames
    assert g.qa.creative_score == pytest.approx(7.80)  # .2·8 + .2·7 + .15·(8+7+9+8)
    req = next(r for r in rt._provider_override.requests if r.template.startswith("qa_ad_critic@"))
    texts = [p.text for p in req.messages[0].parts if hasattr(p, "text")]
    assert texts[0] == "Image 0: the real product photo (the reference)"
    assert sum(1 for t in texts if t.startswith("Image ") and " shot 4 at " in t) == 3
    body = "\n".join(texts)
    assert "story_turn (Story and turn)" in body and "Shot 1 (0-3 s)" in body


async def test_a_low_creative_score_warns_and_spends_nothing_more(tmp_path: Path) -> None:
    low = verdict_output(
        rubric={
            "insight": 3,
            "hook": 4,
            "story_turn": 3,
            "originality": 4,
            "cultural_meaning": 3,
            "product_role": 6,
        }
    )
    orch, rt = llm_run(tmp_path, [{"output": low}])
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    state = await orch.run_until_blocked(run.id)
    assert state.stage is Stage.DONE  # hard gate passed: warn only
    assert mock_provider(rt, state).calls == 1
    creative = [e for e in events(rt, run.id) if "creative_score" in e.payload]
    assert creative and creative[0].level == "warn" and "→ WARN" in creative[0].summary


async def test_a_minor_product_issue_is_made_hard_and_flags_the_ad(tmp_path: Path) -> None:
    bad = verdict_output(
        issues=[
            {
                "category": "product",
                "severity": "minor",
                "observation": "the cap is silver in shot 3",
                "fix": "keep the gold dome cap",
                "frame_index": 8,
            }
        ]
    )
    orch, rt = llm_run(tmp_path, [{"output": bad}])
    run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
    state = await orch.run_until_blocked(run.id)
    g = state.generation
    assert state.stage is Stage.DONE_PARTIAL and g is not None and g.status == "flagged"
    assert g.qa is not None and not g.qa.checks.continuity_ok
    assert g.qa.issues[0].severity == "major" and state.final is not None
    assert mock_provider(rt, state).calls == 1  # nothing regenerated


async def test_inconsistent_verdicts_go_through_schema_repair(tmp_path: Path) -> None:
    unexplained = verdict_output(checks=CHECKS | {"text_free": False})
    wrong_frame = verdict_output(
        issues=[
            {
                "category": "lighting",
                "severity": "minor",
                "observation": "x",
                "fix": "y",
                "frame_index": 40,
            }
        ]
    )
    missing_shot = verdict_output(shots=verdict_output()["shots"][:3])
    good = {"output": verdict_output()}
    for bad in (unexplained, wrong_frame, missing_shot):
        orch, rt = llm_run(tmp_path / str(id(bad)), [{"output": copy.deepcopy(bad)}, good])
        run = orch.create_run(REQUEST, interactive=False, product_photo=photo(tmp_path))
        state = await orch.run_until_blocked(run.id)
        assert state.stage is Stage.DONE
        repairs = [
            e
            for e in events(rt, run.id)
            if e.type in {EventType.LLM_CALL, EventType.LLM_REPAIR}
            and e.payload.get("template") == "qa_ad_critic@v1"
        ]
        assert len(repairs) == 2  # one invalid answer, one repaired
