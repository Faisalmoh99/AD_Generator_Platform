"""Anthropic adapter against the INSTALLED SDK's own types (no network)."""

from __future__ import annotations

from typing import Any

import anthropic
import httpx2  # anthropic 2.x is built on httpx2, not httpx
import pytest
from anthropic.types.beta import BetaMessage

from adsmith.agent.recovery.classifier import classify_anthropic
from adsmith.agent.recovery.errors import (
    PermanentProviderError,
    RateLimitedError,
    TransientProviderError,
)
from adsmith.providers.llm.anthropic import FALLBACK_BETA, build_params, parse_response
from adsmith.providers.llm.base import AssistantMessage, LLMRequest, TextPart, UserMessage


def req(**over: Any) -> LLMRequest:
    base: dict[str, Any] = {
        "system": "static system",
        "messages": (UserMessage(parts=(TextPart("ctx", cache=True), TextPart("task"))),),
        "json_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "schema_name": "X",
        "model": "claude-sonnet-5-5",
        "effort": "high",
        "max_tokens": 16000,
    }
    return LLMRequest(**(base | over))


def test_sdk_version_is_the_one_we_verified() -> None:
    major = int(anthropic.__version__.split(".")[0])
    assert major == 2, "adapter verified against anthropic 2.x; re-verify before upgrading"


def test_params_use_output_config_effort_and_format_and_no_sampling() -> None:
    p = build_params(req())
    assert p["output_config"]["effort"] == "high"
    assert p["output_config"]["format"]["type"] == "json_schema"
    assert "temperature" not in p and "thinking" not in p and "tool_choice" not in p


def test_cache_breakpoints_on_system_and_context_only() -> None:
    p = build_params(req())
    assert p["system"][0]["cache_control"] == {"type": "ephemeral"}
    ctx, task = p["messages"][0]["content"]
    assert ctx["cache_control"] == {"type": "ephemeral"} and "cache_control" not in task
    assert "cache_control" not in build_params(req(cache_system=False))["system"][0]


def test_fallback_only_when_allowed() -> None:
    assert "fallbacks" not in build_params(req())
    p = build_params(req(allow_server_fallback=True))
    assert p["fallbacks"] == "default" and p["betas"] == [FALLBACK_BETA]


def test_assistant_native_blocks_replayed_unchanged() -> None:
    native = [
        {"type": "thinking", "thinking": "", "signature": "sig=="},
        {"type": "text", "text": "{bad json"},
    ]
    p = build_params(
        req(
            messages=(
                UserMessage(parts=(TextPart("task"),)),
                AssistantMessage(text="{bad json", native=native),
                UserMessage(parts=(TextPart("fix it"),)),
            )
        )
    )
    assert p["messages"][1] == {"role": "assistant", "content": native}
    assert [m["role"] for m in p["messages"]] == ["user", "assistant", "user"]


def _beta_message(**over: Any) -> BetaMessage:
    base: dict[str, Any] = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "abc"},
            {"type": "text", "text": '{"ok": true}'},
        ],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 120,
            "output_tokens": 340,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 900,
        },
    }
    return BetaMessage.model_validate(base | over)


def test_parse_response_usage_text_and_native() -> None:
    r = parse_response(_beta_message(), latency_ms=42)
    assert r.text == '{"ok": true}' and r.stop_reason == "end_turn"
    assert (r.usage.input_tokens, r.usage.cache_read_tokens, r.usage.output_tokens) == (
        120,
        900,
        340,
    )
    assert r.native_assistant[0] == {"type": "thinking", "thinking": "", "signature": "abc"}
    assert "citations" not in r.native_assistant[1]  # None fields are not replayed


def test_parse_refusal_and_served_model() -> None:
    msg = _beta_message(
        content=[],
        stop_reason="refusal",
        model="claude-opus-4-8",
        stop_details={"type": "refusal", "category": "cyber", "explanation": None},
    )
    r = parse_response(msg, latency_ms=1)
    assert r.stop_reason == "refusal" and r.refusal_category == "cyber"
    assert r.served_model == "claude-opus-4-8" and r.text is None


def _status_error(
    cls: type[anthropic.APIStatusError], code: int, headers: dict[str, str] | None = None
) -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(code, request=request, headers=headers or {})
    return cls("boom", response=response, body=None)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_status_error(anthropic.RateLimitError, 429, {"retry-after": "7"}), RateLimitedError),
        (_status_error(anthropic.InternalServerError, 500), TransientProviderError),
        (_status_error(anthropic.APIStatusError, 529), TransientProviderError),
        (_status_error(anthropic.BadRequestError, 400), PermanentProviderError),
        (_status_error(anthropic.AuthenticationError, 401), PermanentProviderError),
        (
            anthropic.APITimeoutError(request=httpx2.Request("POST", "https://x")),
            TransientProviderError,
        ),
        (
            anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x")),
            TransientProviderError,
        ),
    ],
    ids=lambda v: type(v).__name__,
)
def test_classifier(exc: Exception, expected: type[Exception]) -> None:
    out = classify_anthropic(exc)
    assert isinstance(out, expected)
    if isinstance(out, RateLimitedError):
        assert out.retry_after_s == 7.0
