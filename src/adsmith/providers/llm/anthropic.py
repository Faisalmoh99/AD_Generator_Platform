"""Anthropic provider (default). Verified against the installed SDK (anthropic 2.4.0):

- Structured output: ``output_config={"format": {"type": "json_schema", "schema": ...}}`` on
  ``messages.create``. The schema comes from ``anthropic.transform_schema`` (public), which moves
  constraints the API cannot enforce (min/max length, ranges) into descriptions. Those are
  enforced by our Pydantic validation instead.
- We deliberately do **not** use ``messages.parse()``: in 2.4.0 it validates eagerly inside the SDK
  and raises on failure, discarding both the raw text (needed for the repair turn) and ``usage``
  (needed for cost accounting, since a failed call is still billed).
- ``effort`` goes in ``output_config``. No ``temperature`` (rejected by current models) and no
  ``thinking`` param (adaptive is the default on Sonnet/Haiku/Opus 5.5).
- Refusal: HTTP 200 with ``stop_reason == "refusal"``. Where the model supports it, we opt into
  server-side fallbacks (``fallbacks: "default"``, beta ``server-side-fallback-2026-07-01``).
  ``response.model`` reports which model actually answered, and we price by it.
- SDK retries are off (``max_retries=0``): the recovery layer owns every retry.
"""

from __future__ import annotations

import time
from typing import Any

import anthropic

from adsmith.agent.recovery.classifier import classify_anthropic
from adsmith.domain.llm import TokenUsage
from adsmith.providers.llm.base import (
    AssistantMessage,
    ImagePart,
    LLMRequest,
    LLMResponse,
    StopReason,
    TextPart,
    UserMessage,
)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
_EPHEMERAL: dict[str, str] = {"type": "ephemeral"}


def _text_block(part: TextPart) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "text", "text": part.text}
    if part.cache:
        block["cache_control"] = _EPHEMERAL
    return block


def _user_content(msg: UserMessage) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    for part in msg.parts:
        if isinstance(part, ImagePart):
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": part.media_type,
                        "data": part.data_b64,
                    },
                }
            )
        else:
            content.append(_text_block(part))
    return content


def build_params(req: LLMRequest) -> dict[str, Any]:
    """Pure request construction (unit-tested without network)."""
    messages: list[dict[str, Any]] = []
    for msg in req.messages:
        if isinstance(msg, AssistantMessage):
            content = msg.native if msg.native is not None else [{"type": "text", "text": msg.text}]
            messages.append({"role": "assistant", "content": content})
        else:
            messages.append({"role": "user", "content": _user_content(msg)})
    system: dict[str, Any] = {"type": "text", "text": req.system}
    if req.cache_system:
        system["cache_control"] = _EPHEMERAL
    params: dict[str, Any] = {
        "model": req.model,
        "max_tokens": req.max_tokens,
        "system": [system],
        "messages": messages,
        "output_config": {
            "effort": req.effort,
            "format": {"type": "json_schema", "schema": req.json_schema},
        },
    }
    if req.allow_server_fallback:
        params["betas"] = [FALLBACK_BETA]
        params["fallbacks"] = "default"
    return params


def _stop(reason: str | None) -> StopReason:
    if reason in {"end_turn", "max_tokens", "refusal"}:
        return reason  # type: ignore[return-value]
    return "other"


def parse_response(resp: Any, *, latency_ms: int) -> LLMResponse:
    """Pure response mapping (unit-tested with SDK model objects)."""
    text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
    u = resp.usage
    usage = TokenUsage(
        input_tokens=u.input_tokens or 0,
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", None) or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", None) or 0,
        output_tokens=u.output_tokens or 0,
    )
    details = getattr(resp, "stop_details", None)
    return LLMResponse(
        text=text,
        stop_reason=_stop(resp.stop_reason),
        raw_stop_reason=resp.stop_reason,
        usage=usage,
        served_model=resp.model,
        latency_ms=latency_ms,
        refusal_category=getattr(details, "category", None) if details is not None else None,
        native_assistant=[b.to_dict(mode="json", exclude_none=True) for b in resp.content],
    )


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, *, timeout_s: float) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=0, timeout=timeout_s)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        params = build_params(request)
        started = time.monotonic()
        try:
            resp = await self._client.beta.messages.create(**params)
        except anthropic.AnthropicError as exc:
            raise classify_anthropic(exc) from exc
        return parse_response(resp, latency_ms=int((time.monotonic() - started) * 1000))
