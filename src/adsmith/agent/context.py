"""Process-level wiring: settings, storage, prompts and LLM callers (one per provider/scenario)."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field

from adsmith.agent.prompts import PromptLibrary
from adsmith.agent.structured import StructuredCaller
from adsmith.config import Settings
from adsmith.domain.run import RunState
from adsmith.providers.llm.base import LLMProvider
from adsmith.providers.llm.scripted import ScriptedLLMProvider
from adsmith.storage import Storage


@dataclass(frozen=True, slots=True)
class NodeContext:
    """What a node may use. Note what is absent: storage. Nodes cannot write state."""

    run_id: str
    settings: Settings
    prompts: PromptLibrary
    llm: StructuredCaller


@dataclass
class AgentRuntime:
    settings: Settings
    storage: Storage
    prompts: PromptLibrary
    rng: random.Random = field(default_factory=random.Random)
    _callers: dict[str, StructuredCaller] = field(default_factory=dict)
    _provider_override: LLMProvider | None = None

    @classmethod
    def from_settings(
        cls, settings: Settings, *, llm_provider: LLMProvider | None = None
    ) -> AgentRuntime:
        return cls(
            settings=settings,
            storage=Storage.open(settings.data_dir),
            prompts=PromptLibrary(settings.prompts_dir),
            _provider_override=llm_provider,
        )

    def _make_provider(self, run: RunState) -> LLMProvider:
        if self._provider_override is not None:
            return self._provider_override
        provider = run.config.llm_provider
        if provider == "scripted":
            return ScriptedLLMProvider.from_scenario(run.config.scripted_scenario or "perfume_en")
        if provider == "anthropic":
            from adsmith.providers.llm.anthropic import AnthropicProvider  # noqa: PLC0415

            key = self.settings.anthropic_api_key
            if key is None:
                raise RuntimeError("ANTHROPIC_API_KEY is required for the anthropic provider")
            return AnthropicProvider(key.get_secret_value(), timeout_s=self.settings.llm_timeout_s)
        from adsmith.providers.llm.openai import OpenAIProvider  # noqa: PLC0415

        okey = self.settings.openai_api_key
        if okey is None:
            raise RuntimeError("OPENAI_API_KEY is required for the openai provider")
        return OpenAIProvider(okey.get_secret_value(), timeout_s=self.settings.llm_timeout_s)

    def node_context(self, run: RunState) -> NodeContext:
        key = f"{run.config.llm_provider}:{run.config.scripted_scenario}"
        if key not in self._callers:
            self._callers[key] = StructuredCaller(
                provider=self._make_provider(run),
                settings=self.settings,
                prompts=self.prompts,
                sleep=asyncio.sleep,
                rng=self.rng,
            )
        return NodeContext(
            run_id=run.id, settings=self.settings, prompts=self.prompts, llm=self._callers[key]
        )
