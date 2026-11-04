"""Opt-in smoke tests against real APIs: ``uv run pytest --live -m live`` (needs ANTHROPIC_API_KEY).

Verifies what unit tests can't: the model IDs in config exist, structured output works end-to-end
through our adapter, usage is reported and priced, and prompt caching actually produces cache
reads on a repeated prefix of at least 512 tokens.
"""

from __future__ import annotations

import asyncio
import os
import random

import anthropic
import pytest
from pydantic import BaseModel, Field

from adsmith.agent.prompts import PromptLibrary, RenderedPrompt
from adsmith.agent.structured import StructuredCaller
from adsmith.config import PROJECT_ROOT, LLMNode, Settings
from adsmith.providers.llm.anthropic import AnthropicProvider

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set"),
]


class Tagline(BaseModel):
    tagline: str = Field(min_length=3, max_length=60)
    language: str


# ~800 tokens of static guidance, above the 512-token minimum cacheable prefix.
STATIC_SYSTEM = "You write short perfume taglines. " + " ".join(
    f"Rule {i}: keep it evocative, concise, and free of competitor brand names." for i in range(80)
)


async def test_configured_model_ids_exist() -> None:
    settings = Settings(llm_provider="anthropic")
    client = anthropic.AsyncAnthropic()
    for model_id in settings.catalog.llm_models:
        model = await client.models.retrieve(model_id)
        assert model.id == model_id


async def test_structured_call_usage_cost_and_cache_reads() -> None:
    settings = Settings(llm_provider="anthropic")
    key = settings.anthropic_api_key
    assert key is not None
    caller = StructuredCaller(
        provider=AnthropicProvider(key.get_secret_value(), timeout_s=120),
        settings=settings,
        prompts=PromptLibrary(PROJECT_ROOT / "prompts"),
        sleep=asyncio.sleep,
        rng=random.Random(),
    )
    prompt = RenderedPrompt(
        template_id="live_tagline@v1",
        system=STATIC_SYSTEM,
        context=None,
        user="Write one tagline for a luxury men's perfume called NOIR.",
        cache_system=True,
    )
    first = await caller.call(LLMNode.EDIT_PARSER, prompt, Tagline)  # Haiku in balanced
    second = await caller.call(LLMNode.EDIT_PARSER, prompt, Tagline)
    assert first.value.tagline and second.value.tagline
    assert first.cost_usd > 0
    c1, c2 = first.calls[-1], second.calls[-1]
    assert c1.usage.cache_write_tokens + c1.usage.cache_read_tokens > 0, "prefix was not cached"
    assert c2.usage.cache_read_tokens > 0, "second identical prefix did not read from cache"
