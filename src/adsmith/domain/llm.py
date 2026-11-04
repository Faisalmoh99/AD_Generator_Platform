"""Record of every LLM call: tokens, cost, outcome. Feeds the event trace and the budget ledger."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from adsmith.domain.base import DomainModel, new_id
from adsmith.domain.enums import Stage
from adsmith.domain.events import AgentEvent, EventType

CallKind = Literal["initial", "repair", "truncation_retry", "transient_retry"]
CallOutcome = Literal["ok", "invalid", "refusal", "truncated", "error"]


class TokenUsage(DomainModel):
    """Billing buckets as reported by the provider. ``input_tokens`` excludes cache reads/writes."""

    input_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)  # includes thinking tokens (billed as output)

    @property
    def total_input(self) -> int:
        return self.input_tokens + self.cache_write_tokens + self.cache_read_tokens


class LLMCallRecord(DomainModel):
    id: str = Field(default_factory=lambda: new_id("llm"))
    node: str
    template: str = Field(description="name@version")
    requested_model: str
    served_model: str = Field(description="May differ when a server-side fallback answered.")
    kind: CallKind
    attempt: int = Field(ge=1)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float = Field(default=0.0, ge=0)
    priced_conservatively: bool = Field(
        default=False, description="Served model had no price entry; charged at the max price."
    )
    latency_ms: int = Field(default=0, ge=0)
    simulated: bool = Field(default=False, description="Scripted provider: tokens are estimates.")
    stop_reason: str | None = None
    outcome: CallOutcome
    error: str | None = Field(default=None, max_length=2000)

    def as_event(self, run_id: str, stage: Stage, scene_id: str | None = None) -> AgentEvent:
        u = self.usage
        fallback = (
            f" (answered by fallback {self.served_model})"
            if self.served_model != self.requested_model
            else ""
        )
        level: Literal["info", "warn", "error"] = "info" if self.outcome == "ok" else "warn"
        return AgentEvent(
            run_id=run_id,
            stage=stage,
            scene_id=scene_id,
            type=EventType.LLM_REPAIR if self.kind == "repair" else EventType.LLM_CALL,
            level=level,
            summary=(
                f"{self.node} via {self.requested_model}{fallback}: {self.outcome}, "
                f"{u.total_input} in ({u.cache_read_tokens} cached) / {u.output_tokens} out, "
                f"${self.cost_usd:.4f}, {self.latency_ms} ms"
            ),
            payload=self.model_dump(mode="json"),
        )
