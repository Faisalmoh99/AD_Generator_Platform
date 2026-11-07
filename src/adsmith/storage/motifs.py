"""The cross-run motif ledger (ADR-0028 part 3): the people, place and object each recent winning
concept stood on, so the next ideate is told what was used and code can steer away from it
(four of five planning runs on 2026-11-07 won with "a host waiting for a guest at the door")."""

from __future__ import annotations

import json

from adsmith.domain.base import utcnow
from adsmith.domain.concept import MotifEntry
from adsmith.storage.db import Database


class MotifLedger:
    def __init__(self, db: Database) -> None:
        self._db = db

    def record(self, entry: MotifEntry, *, source: str = "run") -> None:
        """One row per run (its winning concept); a later winner of the same run replaces it."""
        with self._db.write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO motifs (run_id, created_at, title, axis, motifs, source) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (entry.run_id, utcnow().isoformat(), entry.title, entry.axis,
                 json.dumps(entry.motifs, ensure_ascii=False), source),
            )  # fmt: skip

    def recent(self, limit: int = 10, *, exclude: str | None = None) -> list[MotifEntry]:
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT run_id, title, axis, motifs FROM motifs WHERE run_id != ? "
                "ORDER BY created_at DESC LIMIT ?",
                (exclude or "", limit),
            ).fetchall()
        return [
            MotifEntry(run_id=r["run_id"], title=r["title"], axis=r["axis"],
                       motifs=json.loads(r["motifs"]))
            for r in rows
        ]  # fmt: skip
