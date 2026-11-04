"""Append-only event store.

``seq`` guarantees (what SSE resume relies on), ADR-0011:
- **Strictly monotonic and gap-free per run.** ``seq`` is computed as ``MAX(seq) + 1`` *inside* the
  same ``BEGIN IMMEDIATE`` transaction that inserts the row. SQLite allows one writer at a time, so
  two appenders can never read the same max. A rolled-back transaction consumes no numbers (unlike
  an autoincrement sequence), so there are no gaps.
- **Commit order = seq order.** Because writers are serialised, a reader can never see ``seq=11``
  committed while ``seq=10`` is still in flight. ``tail(after_seq=n)`` therefore never skips an
  event, and a client that reconnects with ``Last-Event-ID: n`` gets exactly the events it missed.
- ``PRIMARY KEY (run_id, seq)`` turns any violation of the above into a hard error.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence

from adsmith.domain.events import AgentEvent
from adsmith.storage.db import Database


def insert_events(
    conn: sqlite3.Connection, run_id: str, events: Sequence[AgentEvent]
) -> list[AgentEvent]:
    """Assign seqs and insert. Must be called inside ``Database.write()``."""
    if not events:
        return []
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) FROM events WHERE run_id = ?", (run_id,)
    ).fetchone()
    next_seq = int(row[0]) + 1
    stored: list[AgentEvent] = []
    for event in events:
        if event.run_id != run_id:
            raise ValueError(f"event for run {event.run_id!r} appended to run {run_id!r}")
        ev = event.model_copy(update={"seq": next_seq})
        conn.execute(
            "INSERT INTO events (run_id, seq, ts, stage, scene_id, type, level, summary, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                ev.seq,
                ev.ts.isoformat(),
                ev.stage.value,
                ev.scene_id,
                ev.type.value,
                ev.level,
                ev.summary,
                json.dumps(ev.payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        stored.append(ev)
        next_seq += 1
    return stored


def _row_to_event(row: sqlite3.Row) -> AgentEvent:
    return AgentEvent.model_validate(
        {
            "seq": row["seq"],
            "run_id": row["run_id"],
            "ts": row["ts"],
            "stage": row["stage"],
            "scene_id": row["scene_id"],
            "type": row["type"],
            "level": row["level"],
            "summary": row["summary"],
            "payload": json.loads(row["payload"]),
        }
    )


class EventStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def append(self, run_id: str, events: Sequence[AgentEvent]) -> list[AgentEvent]:
        """Append events not tied to a state change (e.g. an LLM call log)."""
        with self._db.write() as conn:
            return insert_events(conn, run_id, events)

    def tail(self, run_id: str, after_seq: int = 0, limit: int = 500) -> list[AgentEvent]:
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (run_id, after_seq, limit),
            ).fetchall()
        return [_row_to_event(r) for r in rows]

    def last_seq(self, run_id: str) -> int:
        with self._db.read() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM events WHERE run_id = ?", (run_id,)
            ).fetchone()
        return int(row[0])
