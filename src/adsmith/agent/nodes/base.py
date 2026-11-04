"""Node contract: ``async (state snapshot, NodeContext) -> NodeResult``.

Nodes read a snapshot, may call the LLM, and return typed deltas + events + LLM call records.
They never write state and never choose the next stage.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from adsmith.agent.context import NodeContext
from adsmith.agent.deltas import Delta
from adsmith.domain.events import AgentEvent
from adsmith.domain.llm import LLMCallRecord
from adsmith.domain.run import RunState


@dataclass(slots=True)
class NodeResult:
    deltas: list[Delta] = field(default_factory=list)
    events: list[AgentEvent] = field(default_factory=list)
    llm_calls: list[LLMCallRecord] = field(default_factory=list)


Node = Callable[[RunState, NodeContext], Awaitable[NodeResult]]
