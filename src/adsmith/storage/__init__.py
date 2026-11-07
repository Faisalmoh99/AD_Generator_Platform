"""Persistence: SQLite (WAL) for runs, snapshots, events, cache; content-addressed files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from adsmith.storage.artifacts import ArtifactStore
from adsmith.storage.cache import CacheStore
from adsmith.storage.db import Database
from adsmith.storage.events import EventStore
from adsmith.storage.motifs import MotifLedger
from adsmith.storage.repo import (
    ConcurrencyConflictError,
    RunNotFoundError,
    RunRepository,
    StaleWriteError,
    UpdateResult,
)

__all__ = [
    "ArtifactStore",
    "CacheStore",
    "ConcurrencyConflictError",
    "Database",
    "EventStore",
    "MotifLedger",
    "RunNotFoundError",
    "RunRepository",
    "StaleWriteError",
    "Storage",
    "UpdateResult",
]


@dataclass(frozen=True, slots=True)
class Storage:
    """Everything persistence-related, wired once per process."""

    db: Database
    runs: RunRepository
    events: EventStore
    artifacts: ArtifactStore
    cache: CacheStore
    motifs: MotifLedger

    @classmethod
    def open(cls, data_dir: Path) -> Storage:
        db = Database(data_dir / "adsmith.db")
        artifacts = ArtifactStore(data_dir)
        return cls(
            db=db,
            runs=RunRepository(db),
            events=EventStore(db),
            artifacts=artifacts,
            cache=CacheStore(db, artifacts),
            motifs=MotifLedger(db),
        )
