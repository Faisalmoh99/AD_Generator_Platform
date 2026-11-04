"""StructuredCaller: validation, same-conversation repair, refusal, truncation, retries."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from adsmith.agent.recovery.errors import (
    ContentPolicyError,
    InvalidLLMOutputError,
    PermanentProviderError,
    TransientProviderError,
)
from adsmith.config import LLMNode, Settings
from adsmith.domain.brief import BriefDraft
from adsmith.domain.concept import ConceptSetDraft
from adsmith.domain.storyboard import Storyboard, StoryboardDraft
from adsmith.providers.llm.base import AssistantMessage, TextPart, UserMessage
from adsmith.providers.llm.scripted import FIXTURES_DIR
from tests.llm_helpers import Harness, make_harness

EN = FIXTURES_DIR / "perfume_en"


def fixture_output(template: str) -> dict[str, Any]:
    out: dict[str, Any] = json.loads((EN / f"{template}.json").read_text())[0]["output"]
    return out


GOOD_CONCEPTS = fixture_output("ideate")
GOOD_BRIEF = fixture_output("understand")
GOOD_STORYBOARD = fixture_output("storyboard")


def harness(settings: Settings, tmp_path: Path, **scripts: list[dict[str, Any]]) -> Harness:
    return make_harness(settings, tmp_path, scripts)


def render(h: Harness, name: str = "ideate") -> Any:
    return h.prompts.get(name).render(shared="the bible", task="do it")


async def test_valid_first_try(settings: Settings, tmp_path: Path) -> None:
    h = harness(settings, tmp_path, ideate=[{"output": GOOD_CONCEPTS}])
    res = await h.caller.call(LLMNode.IDEATE, render(h), ConceptSetDraft)
    assert len(res.value.concepts) == 3
    (call,) = res.calls
    assert (call.kind, call.outcome, call.attempt) == ("initial", "ok", 1)
    assert call.usage.input_tokens > 0 and call.cost_usd > 0 and call.simulated


async def test_repair_appends_to_same_conversation(settings: Settings, tmp_path: Path) -> None:
    bad = '{"concepts": [{"title": "only one"}]}'
    h = harness(settings, tmp_path, ideate=[{"raw": bad}, {"output": GOOD_CONCEPTS}])
    res = await h.caller.call(LLMNode.IDEATE, render(h), ConceptSetDraft)
    assert [c.outcome for c in res.calls] == ["invalid", "ok"]
    assert [c.kind for c in res.calls] == ["initial", "repair"]

    first, second = h.provider.requests
    assert len(first.messages) == 1
    original, assistant, repair = second.messages
    assert original == first.messages[0]  # history is append-only: first turn byte-identical
    assert isinstance(assistant, AssistantMessage) and assistant.text == bad
    assert isinstance(repair, UserMessage)
    repair_text = repair.parts[0].text  # type: ignore[union-attr]
    assert "concepts:" in repair_text or "concepts." in repair_text
    assert "Keep everything that was already correct" in repair_text


async def test_domain_invariant_failure_is_repaired(settings: Settings, tmp_path: Path) -> None:
    dup = json.loads(json.dumps(GOOD_CONCEPTS))
    dup["concepts"][2] = dict(dup["concepts"][0], title="The Night Drive II")
    h = harness(settings, tmp_path, ideate=[{"output": dup}, {"output": GOOD_CONCEPTS}])
    res = await h.caller.call(LLMNode.IDEATE, render(h), ConceptSetDraft)
    assert [c.outcome for c in res.calls] == ["invalid", "ok"]
    repair = h.provider.requests[1].messages[-1]
    assert "not distinct" in repair.parts[0].text  # type: ignore[union-attr]


async def test_convert_step_errors_feed_repair(settings: Settings, tmp_path: Path) -> None:
    short = json.loads(json.dumps(GOOD_STORYBOARD))
    short["scenes"][0]["duration_s"] = 2.0  # total 13.5s ≠ 15s
    h = harness(settings, tmp_path, storyboard=[{"output": short}, {"output": GOOD_STORYBOARD}])

    def convert(d: StoryboardDraft) -> Storyboard:
        return Storyboard.from_draft(d, concept_id="cpt", target_duration_s=15.0)

    res = await h.caller.call_and_convert(
        LLMNode.STORYBOARD, render(h, "storyboard"), StoryboardDraft, convert
    )
    assert res.value.total_duration_s == 15.0
    repair = h.provider.requests[1].messages[-1]
    assert "Adjust durations" in repair.parts[0].text  # type: ignore[union-attr]


async def test_repairs_exhausted_raises_with_all_calls(settings: Settings, tmp_path: Path) -> None:
    h = harness(settings, tmp_path, ideate=[{"raw": "not json"}])
    with pytest.raises(InvalidLLMOutputError) as exc:
        await h.caller.call(LLMNode.IDEATE, render(h), ConceptSetDraft)
    assert len(exc.value.llm_calls) == 1 + settings.llm_max_repairs
    assert all(c.outcome == "invalid" for c in exc.value.llm_calls)
    assert exc.value.validation_errors and exc.value.raw == "not json"


async def test_refusal_is_typed_and_booked(settings: Settings, tmp_path: Path) -> None:
    h = harness(settings, tmp_path, ideate=[{"refusal": "cyber"}])
    with pytest.raises(ContentPolicyError) as exc:
        await h.caller.call(LLMNode.IDEATE, render(h), ConceptSetDraft)
    assert exc.value.category == "cyber"
    assert [c.outcome for c in exc.value.llm_calls] == ["refusal"]


async def test_truncation_retries_with_more_tokens(settings: Settings, tmp_path: Path) -> None:
    h = harness(settings, tmp_path, ideate=[{"truncated": True}, {"output": GOOD_CONCEPTS}])
    res = await h.caller.call(LLMNode.IDEATE, render(h), ConceptSetDraft)
    first, second = h.provider.requests
    assert second.max_tokens == 2 * first.max_tokens
    assert len(second.messages) == 1  # cut-off output is not replayed
    assert [c.kind for c in res.calls] == ["initial", "truncation_retry"]


async def test_transient_errors_back_off_then_succeed(settings: Settings, tmp_path: Path) -> None:
    h = harness(
        settings,
        tmp_path,
        ideate=[{"error": "transient"}, {"error": "transient"}, {"output": GOOD_CONCEPTS}],
    )
    res = await h.caller.call(LLMNode.IDEATE, render(h), ConceptSetDraft)
    assert len(h.sleeps) == 2 and all(0 <= d <= 2.0 for d in h.sleeps)
    assert [c.kind for c in res.calls] == ["transient_retry", "transient_retry", "initial"]
    assert [c.outcome for c in res.calls] == ["error", "error", "ok"]


async def test_transient_exhausted_carries_records(settings: Settings, tmp_path: Path) -> None:
    h = harness(settings, tmp_path, ideate=[{"error": "transient"}])
    with pytest.raises(TransientProviderError) as exc:
        await h.caller.call(LLMNode.IDEATE, render(h), ConceptSetDraft)
    assert len(h.sleeps) == 3  # 4 attempts → 3 waits
    assert len(exc.value.llm_calls) == 4


async def test_permanent_error_is_not_retried(settings: Settings, tmp_path: Path) -> None:
    h = harness(settings, tmp_path, ideate=[{"error": "permanent"}])
    with pytest.raises(PermanentProviderError):
        await h.caller.call(LLMNode.IDEATE, render(h), ConceptSetDraft)
    assert h.sleeps == [] and h.provider.calls_for("ideate") == 1


async def test_request_shape_cache_and_fallback(settings: Settings, tmp_path: Path) -> None:
    h = harness(
        settings, tmp_path, ideate=[{"output": GOOD_CONCEPTS}], understand=[{"output": GOOD_BRIEF}]
    )
    await h.caller.call(LLMNode.IDEATE, render(h), ConceptSetDraft)
    await h.caller.call(LLMNode.UNDERSTAND, render(h, "understand"), BriefDraft)
    ideate_req, understand_req = h.provider.requests

    assert ideate_req.cache_system is True
    context, user = ideate_req.messages[0].parts  # type: ignore[union-attr]
    assert isinstance(context, TextPart) and context.cache and "the bible" in context.text
    assert isinstance(user, TextPart) and not user.cache
    assert ideate_req.model == "claude-sonnet-5-5" and ideate_req.allow_server_fallback
    assert understand_req.model == "claude-haiku-5-5" and not understand_req.allow_server_fallback
    assert ideate_req.json_schema["additionalProperties"] is False
    assert ideate_req.template == "ideate@v1"


async def test_prompt_cache_can_be_disabled(tmp_path: Path) -> None:
    s = Settings(data_dir=tmp_path / "d", llm_prompt_cache=False)
    h = harness(s, tmp_path, ideate=[{"output": GOOD_CONCEPTS}])
    await h.caller.call(LLMNode.IDEATE, render(h), ConceptSetDraft)
    (req,) = h.provider.requests
    assert req.cache_system is False
    assert not any(p.cache for p in req.messages[0].parts if isinstance(p, TextPart))  # type: ignore[union-attr]
