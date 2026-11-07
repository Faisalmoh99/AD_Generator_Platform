"""``StructuredCaller``: one LLM call → one validated object, or a typed error. Never a crash.

Loop per call (ADR-0012):
1. Send system + user (+ cached context, + images) constrained to the schema's JSON Schema.
2. Transient/rate-limit errors are retried with backoff (``retry_transient``).
3. ``refusal`` → ``ContentPolicyError`` (after any server-side fallback already tried).
4. ``max_tokens`` → retry once with double the token budget (the output was cut, not wrong).
5. Validate with Pydantic, then run ``convert`` (e.g. ``Storyboard.from_draft``) so domain
   invariants JSON Schema cannot express also count as validation failures.
6. On failure: **append** the model's own output (native blocks, unchanged) and a repair turn
   listing the exact errors to the *same* conversation, then retry (max ``llm_max_repairs``).
Every attempt produces an ``LLMCallRecord`` (tokens, cost, outcome). Errors carry the records
spent so far, so failed calls are still booked.
"""

from __future__ import annotations

import functools
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import cache
from typing import Any

import anthropic
from pydantic import BaseModel, ValidationError

from adsmith.agent.pricing import price_llm_call
from adsmith.agent.prompts import PromptLibrary, RenderedPrompt
from adsmith.agent.recovery.backoff import Sleep, retry_transient
from adsmith.agent.recovery.errors import AgentError, ContentPolicyError, InvalidLLMOutputError
from adsmith.config import LLMNode, NodeModel, Settings
from adsmith.domain.budget import USD_DECIMALS
from adsmith.domain.llm import CallKind, CallOutcome, LLMCallRecord
from adsmith.providers.llm.base import (
    AssistantMessage,
    ImagePart,
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    TextPart,
    UserMessage,
)

MAX_ERRORS_SHOWN = 12
MAX_TRUNCATION_RETRIES = 1
MAX_TOKENS_CEILING = 32_000


@dataclass(frozen=True, slots=True)
class StructuredResult[R]:
    value: R
    calls: list[LLMCallRecord] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.calls)


@cache
def json_schema_for(schema: type[BaseModel]) -> dict[str, Any]:
    """Provider-ready JSON Schema. ``anthropic.transform_schema`` (public in 2.4.0) rewrites
    unsupported constraints into descriptions; Pydantic still enforces them on our side."""
    transformed: dict[str, Any] = anthropic.transform_schema(schema.model_json_schema())
    return transformed


def format_errors(exc: ValueError) -> list[str]:
    if not isinstance(exc, ValidationError):
        return [str(exc)]
    out = []
    for err in exc.errors()[:MAX_ERRORS_SHOWN]:
        loc = ".".join(str(p) for p in err["loc"]) or "(whole object)"
        out.append(f"{loc}: {err['msg']}")
    if exc.error_count() > MAX_ERRORS_SHOWN:
        out.append(f"... and {exc.error_count() - MAX_ERRORS_SHOWN} more errors")
    return out


@dataclass(slots=True)
class _CallState:
    """Mutable bookkeeping for one logical structured call."""

    node: LLMNode
    prompt: RenderedPrompt
    model: NodeModel
    messages: list[LLMMessage]
    max_tokens: int
    calls: list[LLMCallRecord] = field(default_factory=list)
    attempt: int = 0
    repairs: int = 0
    truncations: int = 0
    kind: CallKind = "initial"


class StructuredCaller:
    def __init__(
        self,
        *,
        provider: LLMProvider,
        settings: Settings,
        prompts: PromptLibrary,
        sleep: Sleep,
        rng: random.Random,
    ) -> None:
        self.provider = provider
        self.settings = settings
        self.prompts = prompts
        self._sleep = sleep
        self._rng = rng

    async def call[D: BaseModel](
        self,
        node: LLMNode,
        prompt: RenderedPrompt,
        schema: type[D],
        *,
        images: Sequence[ImagePart | TextPart] = (),  # labelled images allowed
    ) -> StructuredResult[D]:
        return await self.call_and_convert(node, prompt, schema, lambda d: d, images=images)

    async def call_and_convert[D: BaseModel, R](
        self,
        node: LLMNode,
        prompt: RenderedPrompt,
        schema: type[D],
        convert: Callable[[D], R],
        *,
        images: Sequence[ImagePart | TextPart] = (),  # labelled images allowed
    ) -> StructuredResult[R]:
        cache_on = self.settings.llm_prompt_cache
        first: list[TextPart | ImagePart] = []
        if prompt.context:
            first.append(TextPart(prompt.context, cache=cache_on))
        first.extend(images)
        first.append(TextPart(prompt.user))
        st = _CallState(
            node=node,
            prompt=prompt,
            model=self.settings.node_model(node),
            messages=[UserMessage(parts=tuple(first))],
            max_tokens=self.settings.llm_max_tokens,
        )
        while True:
            st.attempt += 1
            resp = await self._send(st, schema)
            if resp.stop_reason == "refusal":
                self._log(st, resp, "refusal")
                raise ContentPolicyError(
                    f"{node} refused (category: {resp.refusal_category or 'unspecified'})",
                    category=resp.refusal_category,
                    llm_calls=st.calls,
                )
            if resp.stop_reason == "max_tokens":
                self._on_truncation(st, resp)
                continue
            try:
                value = convert(schema.model_validate_json(resp.text or ""))
            except ValueError as exc:  # ValidationError is a ValueError; so are domain invariants
                self._on_invalid(st, resp, exc)
                continue
            self._log(st, resp, "ok")
            return StructuredResult(value=value, calls=st.calls)

    # ------------------------------------------------------------------------ steps
    async def _send(self, st: _CallState, schema: type[BaseModel]) -> LLMResponse:
        spec = self.settings.catalog.llm_models.get(st.model.model)
        request = LLMRequest(
            system=st.prompt.system,
            messages=tuple(st.messages),
            json_schema=json_schema_for(schema),
            schema_name=schema.__name__,
            model=st.model.model,
            effort=st.model.effort,
            max_tokens=st.max_tokens,
            template=st.prompt.template_id,
            cache_system=st.prompt.cache_system and self.settings.llm_prompt_cache,
            allow_server_fallback=bool(
                self.settings.anthropic_server_fallback and spec and spec.server_fallback
            ),
        )

        def on_retry(err: AgentError, n: int, delay: float) -> None:
            self._log(
                st,
                None,
                "error",
                f"{err.message} (retry {n} in {delay:.1f}s)",
                kind="transient_retry",
            )

        try:
            return await retry_transient(
                functools.partial(self.provider.complete, request),
                sleep=self._sleep,
                rng=self._rng,
                on_retry=on_retry,
            )
        except AgentError as exc:
            self._log(st, None, "error", exc.message)
            exc.llm_calls = st.calls + exc.llm_calls
            raise

    def _on_truncation(self, st: _CallState, resp: LLMResponse) -> None:
        self._log(st, resp, "truncated")
        if st.truncations >= MAX_TRUNCATION_RETRIES or st.max_tokens >= MAX_TOKENS_CEILING:
            raise InvalidLLMOutputError(
                f"{st.node} output truncated at max_tokens={st.max_tokens}",
                raw=resp.text,
                llm_calls=st.calls,
            )
        st.truncations += 1
        st.max_tokens = min(st.max_tokens * 2, MAX_TOKENS_CEILING)
        st.kind = "truncation_retry"

    def _on_invalid(self, st: _CallState, resp: LLMResponse, exc: ValueError) -> None:
        errors = format_errors(exc)
        self._log(st, resp, "invalid", "; ".join(errors))
        if st.repairs >= self.settings.max_repairs(st.node):
            raise InvalidLLMOutputError(
                f"{st.node} output invalid after {st.repairs} repair attempt(s)",
                validation_errors=errors,
                raw=resp.text,
                llm_calls=st.calls,
            ) from exc
        st.repairs += 1
        repair_turn = self.prompts.get("schema_repair").render(errors=errors)
        # Same conversation: the model sees its own answer (replayed unchanged) + the errors.
        st.messages.append(AssistantMessage(text=resp.text or "", native=resp.native_assistant))
        st.messages.append(UserMessage(parts=(TextPart(repair_turn.user),)))
        st.kind = "repair"

    def _log(
        self,
        st: _CallState,
        resp: LLMResponse | None,
        outcome: CallOutcome,
        error: str | None = None,
        *,
        kind: CallKind | None = None,
    ) -> None:
        base: dict[str, Any] = {
            "node": st.node.value,
            "template": st.prompt.template_id,
            "requested_model": st.model.model,
            "kind": kind or st.kind,
            "attempt": st.attempt,
            "outcome": outcome,
            "error": error[:2000] if error else None,
        }
        if resp is None:
            record = LLMCallRecord(
                **base,
                served_model=st.model.model,
                simulated=self.provider.name == "scripted",
            )
        else:
            cost, conservative = price_llm_call(
                self.settings.catalog, resp.served_model, resp.usage
            )
            record = LLMCallRecord(
                **base,
                served_model=resp.served_model,
                usage=resp.usage,
                cost_usd=round(cost, USD_DECIMALS),
                priced_conservatively=conservative,
                latency_ms=resp.latency_ms,
                simulated=bool(resp.extras.get("simulated", False)),
                stop_reason=resp.raw_stop_reason,
            )
        st.calls.append(record)
