"""The agent's event trace: an append-only record of every decision, retry and cost."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, JsonValue

from adsmith.domain.base import DomainModel, utcnow
from adsmith.domain.enums import Stage


class EventType(StrEnum):
    STAGE_STARTED = "stage_started"
    STAGE_COMPLETED = "stage_completed"
    LLM_CALL = "llm_call"
    LLM_REPAIR = "llm_repair"
    DECISION = "decision"
    ATTEMPT_STARTED = "attempt_started"
    ATTEMPT_FINISHED = "attempt_finished"
    RETRY_SCHEDULED = "retry_scheduled"
    RECOVERY = "recovery"
    CACHE_HIT = "cache_hit"
    BUDGET = "budget"
    SCENE_STATUS = "scene_status"
    EDIT_PARSED = "edit_parsed"
    BLAST_RADIUS = "blast_radius"
    AWAITING_INPUT = "awaiting_input"
    EXPORT = "export"
    ERROR = "error"
    RUN_COMPLETED = "run_completed"


class AgentEvent(DomainModel):
    """``seq`` is assigned by the event store (monotonic per run); 0 means not yet stored.

    ``summary`` is a one-line, human-readable reasoning summary, not chain-of-thought.
    ``payload`` carries structured details: model, template version, tokens, cost, latency,
    digests of inputs/outputs, chaos flags.
    """

    seq: int = Field(default=0, ge=0)
    run_id: str
    ts: datetime = Field(default_factory=utcnow)
    stage: Stage
    scene_id: str | None = None
    type: EventType
    summary: Annotated[str, Field(min_length=1, max_length=500)]
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    level: Literal["info", "warn", "error"] = "info"
