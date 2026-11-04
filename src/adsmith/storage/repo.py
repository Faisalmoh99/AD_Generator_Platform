"""Run repository: versioned ``RunState`` snapshots with atomic read-modify-write.

Write model (ADR-0011):
- Callers never save a ``RunState`` they are holding. They submit a **mutation function** to
  ``update()``, which inside one ``BEGIN IMMEDIATE`` transaction:
  1. loads the latest snapshot,
  2. (optionally) checks ``expected_version`` (optimistic lock for human commands),
  3. applies the mutation, which may raise to veto, e.g. if a scene result is for an outdated
     scene version,
  4. bumps ``state_version``, re-validates the whole aggregate, and writes a compressed snapshot,
  5. appends the events the mutation returned, with their seqs, in the **same** transaction.
  Any exception rolls back all of it: no snapshot without its events, no events without their state.
- Because the read happens under the write lock, two writers can never both apply to version N.
  The second one sees N+1 and applies its mutation to that. Lost updates are impossible by
  construction. ``ConcurrencyConflictError`` is only raised when a caller *asks* for a specific
  version (a human acting on what they saw), never for automated writers.
"""

from __future__ import annotations

import sqlite3
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from adsmith.domain.base import new_id, utcnow
from adsmith.domain.enums import Stage
from adsmith.domain.events import AgentEvent, EventType
from adsmith.domain.run import RunState
from adsmith.storage.db import Database
from adsmith.storage.events import insert_events

Mutation = Callable[[RunState], Sequence[AgentEvent] | None]
_ZLIB_LEVEL = 6


class RunNotFoundError(KeyError):
    pass


class ConcurrencyConflictError(RuntimeError):
    """The run moved on since the caller looked at it (optimistic lock)."""

    def __init__(self, run_id: str, expected: int, actual: int) -> None:
        super().__init__(f"run {run_id}: expected version {expected}, current is {actual}")
        self.run_id, self.expected, self.actual = run_id, expected, actual


class StaleWriteError(RuntimeError):
    """Raised *by a mutation* to veto a write whose preconditions no longer hold."""


@dataclass(frozen=True, slots=True)
class UpdateResult:
    state: RunState
    events: list[AgentEvent]


@dataclass(frozen=True, slots=True)
class SnapshotInfo:
    state_version: int
    stage: Stage
    reason: str
    created_at: datetime
    size_bytes: int


@dataclass(frozen=True, slots=True)
class RunSummary:
    id: str
    stage: Stage
    state_version: int
    request: str
    created_at: datetime
    updated_at: datetime
    forked_from: str | None


def _encode(state: RunState) -> tuple[bytes, RunState]:
    """Serialise and prove it round-trips: we never store a snapshot we couldn't load back."""
    raw = state.model_dump_json()
    validated = RunState.model_validate_json(raw)  # runs every invariant on the whole aggregate
    return zlib.compress(raw.encode("utf-8"), _ZLIB_LEVEL), validated


def _decode(blob: bytes) -> RunState:
    return RunState.model_validate_json(zlib.decompress(blob))


class RunRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------------ writes
    def create(
        self,
        state: RunState,
        *,
        events: Sequence[AgentEvent] = (),
        forked_from: str | None = None,
    ) -> UpdateResult:
        if state.state_version != 0:
            raise ValueError("new runs start at state_version 0")
        state = state.model_copy(update={"state_version": 1, "updated_at": utcnow()})
        blob, validated = _encode(state)
        with self._db.write() as conn:
            conn.execute(
                "INSERT INTO runs (id, created_at, updated_at, stage, state_version, request,"
                " forked_from) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    validated.id,
                    validated.created_at.isoformat(),
                    validated.updated_at.isoformat(),
                    validated.stage.value,
                    validated.state_version,
                    validated.request,
                    forked_from,
                ),
            )
            self._insert_snapshot(conn, validated, blob, "created")
            stored = insert_events(conn, validated.id, events)
        return UpdateResult(validated, stored)

    def update(
        self,
        run_id: str,
        mutate: Mutation,
        *,
        reason: str,
        expected_version: int | None = None,
    ) -> UpdateResult:
        with self._db.write() as conn:
            current = self._load_latest(conn, run_id)
            if expected_version is not None and current.state_version != expected_version:
                raise ConcurrencyConflictError(run_id, expected_version, current.state_version)
            events = list(mutate(current) or ())
            current.state_version += 1
            current.updated_at = utcnow()
            blob, validated = _encode(current)
            conn.execute(
                "UPDATE runs SET stage = ?, state_version = ?, updated_at = ? WHERE id = ?",
                (
                    validated.stage.value,
                    validated.state_version,
                    validated.updated_at.isoformat(),
                    run_id,
                ),
            )
            self._insert_snapshot(conn, validated, blob, reason)
            stored = insert_events(conn, run_id, events)
        return UpdateResult(validated, stored)

    def fork(self, run_id: str, from_version: int | None = None) -> UpdateResult:
        """New run starting from a past snapshot (``adsmith resume --from-version N``)."""
        source = (
            self.load(run_id) if from_version is None else self.load_version(run_id, from_version)
        )
        now = utcnow()
        fork = source.model_copy(
            update={"id": new_id("run"), "state_version": 0, "created_at": now, "updated_at": now}
        )
        event = AgentEvent(
            run_id=fork.id,
            stage=fork.stage,
            type=EventType.DECISION,
            summary=f"Forked from {run_id} at version {source.state_version} ({source.stage}).",
            payload={"forked_from": run_id, "from_version": source.state_version},
        )
        return self.create(fork, events=[event], forked_from=run_id)

    # ------------------------------------------------------------------------ reads
    def load(self, run_id: str) -> RunState:
        with self._db.read() as conn:
            return self._load_latest(conn, run_id)

    def load_version(self, run_id: str, state_version: int) -> RunState:
        with self._db.read() as conn:
            row = conn.execute(
                "SELECT state_z FROM snapshots WHERE run_id = ? AND state_version = ?",
                (run_id, state_version),
            ).fetchone()
        if row is None:
            raise RunNotFoundError(f"{run_id}@{state_version}")
        return _decode(row["state_z"])

    def history(self, run_id: str) -> list[SnapshotInfo]:
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT state_version, stage, reason, created_at, length(state_z) AS size"
                " FROM snapshots WHERE run_id = ? ORDER BY state_version",
                (run_id,),
            ).fetchall()
        return [
            SnapshotInfo(
                state_version=r["state_version"],
                stage=Stage(r["stage"]),
                reason=r["reason"],
                created_at=datetime.fromisoformat(r["created_at"]),
                size_bytes=r["size"],
            )
            for r in rows
        ]

    def list_runs(self, limit: int = 50) -> list[RunSummary]:
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            RunSummary(
                id=r["id"],
                stage=Stage(r["stage"]),
                state_version=r["state_version"],
                request=r["request"],
                created_at=datetime.fromisoformat(r["created_at"]),
                updated_at=datetime.fromisoformat(r["updated_at"]),
                forked_from=r["forked_from"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------------ internals
    @staticmethod
    def _load_latest(conn: sqlite3.Connection, run_id: str) -> RunState:
        row = conn.execute(
            "SELECT s.state_z FROM runs r JOIN snapshots s"
            " ON s.run_id = r.id AND s.state_version = r.state_version WHERE r.id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise RunNotFoundError(run_id)
        return _decode(row["state_z"])

    @staticmethod
    def _insert_snapshot(
        conn: sqlite3.Connection, state: RunState, blob: bytes, reason: str
    ) -> None:
        conn.execute(
            "INSERT INTO snapshots (run_id, state_version, stage, reason, created_at, state_z)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                state.id,
                state.state_version,
                state.stage.value,
                reason,
                state.updated_at.isoformat(),
                blob,
            ),
        )
