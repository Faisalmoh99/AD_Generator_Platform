"""STOP 1 fixes: composition is HARD for key shots and event shots whose event is the point; a
deliberate change to the product (cap off) is told to the reference model and the gate."""

from __future__ import annotations

from pathlib import Path

from adsmith.agent.context import AgentRuntime
from adsmith.agent.qa import _product_checklist, composition_is_spec, composition_rule
from adsmith.config import Settings
from adsmith.domain.qa import QAChecks, QAIssue, QAVerdict, QAVerdictDraft
from tests.factories import make_full_run, make_scene


def draft(*, spec_ok: bool = True, severity: str = "major") -> QAVerdictDraft:
    checks = QAChecks(
        product_visible=True,
        matches_shot_spec=spec_ok,
        lighting_matches=True,
        continuity_ok=True,
        artifacts_free=True,
        brand_safe=True,
    )
    issue = QAIssue.model_validate(
        {
            "category": "composition",
            "severity": severity,
            "observation": "no ribbon of water anywhere in the frame",
            "fix": "show the ribbon",
        }
    )
    return QAVerdictDraft(score=0.75, checks=checks, issues=[issue], frames_inspected=1)


def test_missing_event_is_soft_by_default_and_hard_for_event_shots() -> None:
    soft = QAVerdict.from_draft(draft(), threshold=0.7)
    assert soft.passed and soft.issues[0].severity == "minor"
    hard = QAVerdict.from_draft(draft(), threshold=0.7, composition_hard=True)
    assert not hard.passed and "composition is the spec" in hard.fail_reasons[0]
    spec = QAVerdict.from_draft(
        draft(spec_ok=False, severity="minor"), threshold=0.7, composition_hard=True
    )
    assert not spec.passed


def test_which_entries_have_their_composition_as_spec(settings: Settings) -> None:
    v = settings.vocabulary
    hard = sorted(n for n, e in v.shots.items() if e.composition_is_spec)
    assert hard == ["bottle_into_water", "hero_push_in", "spray_from_nozzle", "water_ribbon"]
    # Macros are cut from the packshot (STOP 2); the cap lift is a product shot from the packshot.
    assert (v.shots["cap_macro"].identity_mode, v.shots["cap_macro"].crop) == ("crop", "cap")
    assert v.shots["glass_edge_macro"].crop == "edge"
    assert v.shots["cap_lift"].identity_mode == "direct" and v.shots["cap_lift"].reference_note


def test_rule_and_checklist_reach_the_gate(tmp_path: Path) -> None:
    rt = AgentRuntime.from_settings(Settings(data_dir=tmp_path / "d"))
    ctx = rt.node_context(make_full_run())
    ribbon = make_scene(1, vocab="water_ribbon", shows_product=True)
    sweep = make_scene(2, vocab="light_sweep", shows_product=True)
    assert composition_is_spec(ctx, ribbon) and not composition_is_spec(ctx, sweep)
    assert "HARD FOR THIS SHOT" in composition_rule(ctx, ribbon)
    assert "soft" in composition_rule(ctx, sweep)
    state = make_full_run()
    spray = make_scene(3, vocab="spray_from_nozzle", shows_product=True)
    text = _product_checklist(state, spray, ctx)
    assert "BY DESIGN FOR THIS SHOT: the cap is off" in text
    assert "BY DESIGN" not in _product_checklist(state, sweep, ctx)
