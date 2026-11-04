"""Live Rich trace: tails the run's event store while the agent works and prints one readable
line per meaningful event (stages, scene progress, retries, rewrites, QA verdicts, cache hits,
edits, cost). The event store is the single source of truth, so the same view works for a run in
this process or in another one (``adsmith trace --follow``)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from typing import TypeVar

from rich.console import Console
from rich.text import Text

from adsmith.domain.events import AgentEvent, EventType
from adsmith.storage import Storage

T = TypeVar("T")

_ICON: dict[EventType, tuple[str, str]] = {
    EventType.STAGE_STARTED: ("▶", "bold cyan"),
    EventType.STAGE_COMPLETED: ("✓", "green"),
    EventType.DECISION: ("•", "white"),
    EventType.ATTEMPT_FINISHED: ("■", "white"),
    EventType.RETRY_SCHEDULED: ("↻", "yellow"),
    EventType.RECOVERY: ("✎", "magenta"),
    EventType.CACHE_HIT: ("⚡", "cyan"),
    EventType.SCENE_STATUS: ("◆", "green"),
    EventType.EDIT_PARSED: ("✂", "bold magenta"),
    EventType.BLAST_RADIUS: ("◎", "bold magenta"),
    EventType.LLM_CALL: ("·", "dim"),
    EventType.LLM_REPAIR: ("⟲", "yellow"),
    EventType.AWAITING_INPUT: ("?", "bold yellow"),
    EventType.EXPORT: ("⤓", "dim"),
    EventType.ERROR: ("✗", "bold red"),
    EventType.RUN_COMPLETED: ("★", "bold green"),
}
_HIDDEN = {EventType.ATTEMPT_STARTED, EventType.BUDGET}


def format_event(e: AgentEvent, t0: float, *, verbose: bool = False) -> Text | None:
    if e.type in _HIDDEN and not verbose:
        return None
    icon, style = _ICON.get(e.type, ("•", "white"))
    if e.level == "error":
        style = "bold red"
    elif e.level == "warn" and e.type not in {EventType.LLM_CALL}:
        style = "yellow" if e.type is not EventType.RECOVERY else "magenta"
    summary = e.summary
    if e.type is EventType.LLM_CALL:
        p = e.payload
        summary = (
            f"llm {p.get('node')} · {str(p.get('requested_model', '')).replace('claude-', '')} · "
            f"${float(str(p.get('cost_usd', 0))):.4f} · {int(str(p.get('latency_ms', 0)))} ms"
        )
    elif e.type is EventType.DECISION and not verbose and len(summary) > 160:
        summary = summary[:157] + "…"
    if e.payload.get("chaos_injected"):
        summary += "  [chaos]"
    line = Text()
    line.append(f"{max(0.0, time.monotonic() - t0):6.1f}s ", style="dim")
    line.append(f"{icon} ", style=style)
    line.append(f"{e.stage.value:<9} ", style="dim")
    line.append(summary, style=style)
    return line


class LiveTrace:
    def __init__(self, console: Console, storage: Storage, *, verbose: bool = False) -> None:
        self.console, self.storage, self.verbose = console, storage, verbose
        self.t0 = time.monotonic()

    def last_seq(self, run_id: str) -> int:
        return max((e.seq for e in self.storage.events.tail(run_id, limit=1_000_000)), default=0)

    def flush(self, run_id: str, after: int) -> int:
        for e in self.storage.events.tail(run_id, after_seq=after, limit=10_000):
            after = e.seq
            line = format_event(e, self.t0, verbose=self.verbose)
            if line is not None:
                self.console.print(line)
        return after

    async def follow(self, run_id: str, work: Awaitable[T], *, after: int | None = None) -> T:
        """Run ``work`` while printing new events for ``run_id``."""
        seq = self.last_seq(run_id) if after is None else after
        task = asyncio.ensure_future(work)
        while not task.done():
            seq = self.flush(run_id, seq)
            await asyncio.sleep(0.15)
        self.flush(run_id, seq)
        return task.result()
