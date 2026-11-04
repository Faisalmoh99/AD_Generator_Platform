"""OpenAI provider (alternative). Verified against the installed SDK (openai 3.44): Responses API
``responses.create(instructions=..., input=..., text={"format": {"type": "json_schema", "name",
"schema", "strict": True}})``. Model IDs come only from config (``[openai]`` in models.toml, empty
by default, so the provider fails fast until someone sets them from current docs).

Status: implemented to the interface and unit-tested at the request/response mapping level. Not
exercised live in this prototype (see README limitations).
"""

from __future__ import annotations

import time
from typing import Any

import openai

from adsmith.agent.recovery.classifier import classify_openai
from adsmith.agent.recovery.errors import PermanentProviderError
from adsmith.domain.llm import TokenUsage
from adsmith.providers.llm.base import (
    AssistantMessage,
    ImagePart,
    LLMRequest,
    LLMResponse,
    UserMessage,
)

_EFFORT_MAP = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


def build_params(req: LLMRequest) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for msg in req.messages:
        if isinstance(msg, AssistantMessage):
            items.append({"role": "assistant", "content": msg.text})
            continue
        assert isinstance(msg, UserMessage)
        content: list[dict[str, Any]] = []
        for part in msg.parts:
            if isinstance(part, ImagePart):
                url = f"data:{part.media_type};base64,{part.data_b64}"
                content.append({"type": "input_image", "image_url": url})
            else:
                content.append({"type": "input_text", "text": part.text})
        items.append({"role": "user", "content": content})
    return {
        "model": req.model,
        "instructions": req.system,  # OpenAI caches long stable prefixes automatically
        "input": items,
        "max_output_tokens": req.max_tokens,
        "reasoning": {"effort": _EFFORT_MAP[req.effort]},
        "text": {
            "format": {
                "type": "json_schema",
                "name": req.schema_name[:64],
                "schema": req.json_schema,
                "strict": True,
            }
        },
    }


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str, *, timeout_s: float) -> None:
        self._client = openai.AsyncOpenAI(api_key=api_key, max_retries=0, timeout=timeout_s)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if not request.model:
            raise PermanentProviderError("OpenAI model id is empty; set [openai] in models.toml")
        started = time.monotonic()
        try:
            resp = await self._client.responses.create(**build_params(request))
        except openai.OpenAIError as exc:
            raise classify_openai(exc) from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        u = resp.usage
        cached = (u.input_tokens_details.cached_tokens or 0) if u and u.input_tokens_details else 0
        usage = TokenUsage(
            input_tokens=(u.input_tokens - cached) if u else 0,
            cache_read_tokens=cached,
            output_tokens=u.output_tokens if u else 0,
        )
        incomplete = resp.incomplete_details.reason if resp.incomplete_details else None
        stop: Any = "max_tokens" if incomplete == "max_output_tokens" else "end_turn"
        if incomplete == "content_filter":
            stop = "refusal"
        return LLMResponse(
            text=resp.output_text or None,
            stop_reason=stop,
            raw_stop_reason=incomplete or resp.status,
            usage=usage,
            served_model=resp.model,
            latency_ms=latency_ms,
        )
