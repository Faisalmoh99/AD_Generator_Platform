"""Token usage → cost → budget ledger → trace."""

from __future__ import annotations

import pytest

from adsmith.agent.pricing import llm_cost, price_llm_call
from adsmith.config import LLMModelSpec, Settings
from adsmith.domain.budget import BudgetLedger
from adsmith.domain.enums import Stage
from adsmith.domain.events import EventType
from adsmith.domain.llm import LLMCallRecord, TokenUsage
from adsmith.providers.llm.openai import build_params as openai_params
from tests.unit.test_anthropic_provider import req

SONNET = LLMModelSpec(input=2.0, output=10.0, cache_write=2.5, cache_read=0.2)


def test_cost_formula_per_bucket() -> None:
    usage = TokenUsage(
        input_tokens=1_000, cache_write_tokens=2_000, cache_read_tokens=10_000, output_tokens=500
    )
    expected = (1_000 * 2.0 + 2_000 * 2.5 + 10_000 * 0.2 + 500 * 10.0) / 1_000_000
    assert llm_cost(usage, SONNET) == pytest.approx(expected)
    assert expected == pytest.approx(0.014)


def test_cache_read_is_a_tenth_of_input_on_sonnet(settings: Settings) -> None:
    spec = settings.catalog.llm_models["claude-sonnet-5-5"]
    assert spec.cache_read == pytest.approx(spec.input * 0.1)
    assert spec.cache_write == pytest.approx(spec.input * 1.25)


def test_unknown_served_model_is_priced_conservatively(settings: Settings) -> None:
    usage = TokenUsage(input_tokens=1_000_000)
    cost, conservative = price_llm_call(settings.catalog, "claude-mystery", usage)
    assert conservative and cost == pytest.approx(4.0)  # max input price in the catalog
    cost, conservative = price_llm_call(settings.catalog, "claude-haiku-5-5", usage)
    assert not conservative and cost == pytest.approx(0.10)


def _call(cost: float, ref: str = "llm_1") -> LLMCallRecord:
    return LLMCallRecord(
        id=ref,
        node="ideate",
        template="ideate@v1",
        requested_model="claude-sonnet-5-5",
        served_model="claude-sonnet-5-5",
        kind="initial",
        attempt=1,
        usage=TokenUsage(input_tokens=100, cache_read_tokens=900, output_tokens=50),
        cost_usd=cost,
        outcome="ok",
    )


def test_ledger_books_calls_idempotently() -> None:
    ledger = BudgetLedger(limit_usd=10, max_attempts_per_scene=6, max_total_attempts=40)
    for c in (_call(0.01, "a"), _call(0.02, "b"), _call(0.02, "b")):  # "b" replayed
        ledger.record_actual("llm", c.cost_usd, ref=c.id)
    assert ledger.spent_usd == pytest.approx(0.03)
    assert ledger.by_category["llm"] == pytest.approx(0.03)
    assert ledger.remaining_usd == pytest.approx(9.97)
    assert len(ledger.entries) == 2


def test_call_record_becomes_trace_event() -> None:
    ev = _call(0.0123).as_event("run_x", Stage.IDEATE)
    assert ev.type is EventType.LLM_CALL
    assert "1000 in (900 cached)" in ev.summary and "$0.0123" in ev.summary
    usage = ev.payload["usage"]
    assert isinstance(usage, dict) and usage["cache_read_tokens"] == 900


def test_fallback_is_visible_in_trace() -> None:
    c = _call(0.01).model_copy(update={"served_model": "claude-opus-4-8"})
    assert "answered by fallback claude-opus-4-8" in c.as_event("r", Stage.IDEATE).summary


def test_openai_request_shape() -> None:
    p = openai_params(req(model="some-openai-model"))
    assert p["text"]["format"]["type"] == "json_schema" and p["text"]["format"]["strict"] is True
    assert p["instructions"] == "static system" and p["reasoning"] == {"effort": "high"}
    assert p["input"][0]["content"][0] == {"type": "input_text", "text": "ctx"}
